# =============================================================================
# MIT License
#
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

"""
Caption: image captioning loader + facade (vision family).

- CaptionerLoader: load/preprocess/inference/postprocess for image-text-to-text
  VLMs. Natively supported architectures (SmolVLM/Idefics3, Qwen3-VL, ...) load
  via AutoModelForImageTextToText with no trust_remote_code. Models that ship
  their own modeling code (config.json has an ``auto_map``, e.g. Mage-VL) load
  via AutoModelForCausalLM with trust_remote_code=True, and are REQUIRED to be
  pinned to a full commit sha so the executed remote code is immutable.
  Returns a plain caption string (JSON-friendly).
- Captioner: user-facing facade. Uses the model server when --modelserver is
  set, else local. ``caption(image)`` returns a string. ``model_name`` is the
  model identity; ``prompt`` and ``max_new_tokens`` are per-request.
"""

import io
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ai.web.metrics import metrics
from ai.common.utils.image_utils import image_to_bytes
from ai.common.utils.cuda_utils import pick_torch_device, pick_torch_dtype, model_gpu_gb
from ..base import BaseLoader, get_model_server_address, ModelClient

logger = logging.getLogger('rocketlib.models.caption')

DEFAULT_MODEL = 'HuggingFaceTB/SmolVLM-500M-Instruct'
DEFAULT_PROMPT = 'Describe this image in detail.'
DEFAULT_MAX_NEW_TOKENS = 256

# GPU allocation request when the profile doesn't provide memory_gb
# (sized for the SmolVLM-500M default; 4B profiles pass ~10-11 GB).
DEFAULT_MEMORY_GB = 2.0

_FULL_SHA_RE = re.compile(r'^[0-9a-f]{40}$')

# Local-mode watchdog: skip the frame if generation hangs past this.
INFERENCE_TIMEOUT = 60

# Long-edge (px) the input is downscaled to before captioning. SmolVLM's
# processor handles inputs up to ~1536 px well; this is quality-neutral and
# trims the model-server payload on large images.
INFER_MAX_EDGE = 1536


class CaptionerLoader(BaseLoader):
    """Static loader for image-text-to-text VLM captioning (SmolVLM, Qwen3-VL, Mage-VL, ...)."""

    LOADER_TYPE: str = 'caption'
    _REQUIREMENTS_FILE = [
        os.path.join(os.path.dirname(__file__), 'requirements_vision.txt'),
        os.path.join(os.path.dirname(__file__), 'requirements_caption.txt'),
    ]
    _DEFAULTS: dict = {}

    @staticmethod
    def _get_config_dict(model_name: str, revision: Optional[str] = None) -> Dict[str, Any]:
        """Fetch the raw config.json dict for a model WITHOUT executing remote code.

        Args:
            model_name: HF model id.
            revision: Optional pinned model revision.

        Returns:
            The parsed config.json as a plain dict.
        """
        from transformers import PretrainedConfig

        config_dict, _ = PretrainedConfig.get_config_dict(model_name, revision=revision)
        return config_dict

    @staticmethod
    def _select_loading_strategy(config_dict: Dict[str, Any], revision: Optional[str] = None) -> str:
        """Pick the loading path from the model's config — never from its name.

        A config.json with an ``auto_map`` means the repo ships its own modeling
        code and can only load with trust_remote_code=True. That path is gated:
        the revision MUST be a full 40-hex commit sha so the code that gets
        executed is immutable (a branch/tag pin could be moved after review).

        Args:
            config_dict: Raw config.json dict (from _get_config_dict).
            revision: Configured model revision.

        Returns:
            'remote_code' or 'native'.

        Raises:
            ValueError: remote-code model without a full commit-sha revision.
        """
        if config_dict.get('auto_map'):
            if not (revision and _FULL_SHA_RE.match(str(revision))):
                raise ValueError(
                    'caption: this model ships custom modeling code (config.json auto_map) and '
                    'requires trust_remote_code=True; refusing to load without a full 40-hex '
                    f'commit-sha revision pin (got revision={revision!r}). Pin the exact sha in '
                    'the node profile so the executed remote code is immutable.'
                )
            return 'remote_code'
        return 'native'

    @staticmethod
    def load(
        model_name: str = DEFAULT_MODEL,
        device: Optional[str] = None,
        allocate_gpu: Optional[callable] = None,
        exclude_gpus: Optional[List[int]] = None,
        revision: Optional[str] = None,
        memory_gb: Optional[float] = None,
        **kwargs,
    ) -> Tuple[Any, Dict[str, Any], int]:
        """Load a caption VLM (model + processor); bf16 on CUDA, fp32 elsewhere.

        Args:
            model_name: HF model id for the caption model.
            device: Local torch device; ignored when allocate_gpu is provided.
            allocate_gpu: Server callable (memory_gb, exclude_gpus) -> (gpu_index, device).
            exclude_gpus: GPU indices the allocator must avoid.
            revision: Optional pinned model revision (REQUIRED as a full commit
                sha for models that need trust_remote_code).
            memory_gb: GPU memory to request from the allocator (profile-provided;
                defaults to DEFAULT_MEMORY_GB for the small default model).
            **kwargs: Ignored extra loader options.

        Returns:
            Tuple (bundle {'model','processor','device','dtype'}, metadata dict, gpu_index) — -1 on CPU.
        """
        CaptionerLoader._ensure_dependencies()

        from transformers import AutoModelForImageTextToText, AutoProcessor

        config_dict = CaptionerLoader._get_config_dict(model_name, revision=revision)
        strategy = CaptionerLoader._select_loading_strategy(config_dict, revision=revision)

        if allocate_gpu:
            gpu_index, device = allocate_gpu(float(memory_gb or DEFAULT_MEMORY_GB), exclude_gpus or [])
            logger.info(f'Allocated GPU {gpu_index} ({device}) for caption {model_name}')
        else:
            device = device or pick_torch_device()
            gpu_index = int(device.split(':')[1]) if str(device).startswith('cuda:') else -1

        dtype = pick_torch_dtype(device, cuda='bfloat16', mps='float32', cpu='float32')
        if strategy == 'remote_code':
            # ================================================================
            # SECURITY: trust_remote_code=True — this executes python shipped
            # inside the model repo (e.g. microsoft/Mage-VL, whose weights only
            # load through its bundled modeling_mage_vl.py). This violates the
            # house no-trust_remote_code rule and was EXPLICITLY accepted for
            # this loader branch only. Mitigation: _select_loading_strategy has
            # already enforced that ``revision`` is a full commit sha, and that
            # SAME sha is passed to every from_pretrained call below, so the
            # remote code that runs is exactly the reviewed, immutable commit.
            # trust_remote_code must NEVER become a default or leak into the
            # native branch.
            # ================================================================
            from transformers import AutoModelForCausalLM

            model = (
                AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype, revision=revision, trust_remote_code=True)
                .to(device)
                .eval()
            )
            processor = AutoProcessor.from_pretrained(model_name, revision=revision, trust_remote_code=True)
        else:
            # Native path: architecture is registered in transformers, resolved
            # via the auto-mapping (Idefics3/SmolVLM, Qwen3-VL, future VLMs) —
            # no trust_remote_code, no per-model class matching.
            model = (
                AutoModelForImageTextToText.from_pretrained(model_name, dtype=dtype, revision=revision)
                .to(device)
                .eval()
            )
            processor = AutoProcessor.from_pretrained(model_name, revision=revision)

        metadata = {'device': str(device), 'model_name': model_name, 'loader': 'caption'}
        return {'model': model, 'processor': processor, 'device': device, 'dtype': dtype}, metadata, gpu_index

    @staticmethod
    def preprocess(model: Any, inputs: List[Any], metadata: Optional[Dict] = None) -> Dict[str, Any]:
        """Decode image bytes (or accept PIL) to RGB PIL images.

        Args:
            model: Loaded bundle (unused; kept for the loader interface).
            inputs: List of image bytes and/or PIL images.
            metadata: Loader metadata (unused).

        Returns:
            Dict with 'images' (list of RGB PIL images) and 'batch_size'.
        """
        from PIL import Image

        images = []
        for inp in inputs:
            if isinstance(inp, (bytes, bytearray)):
                images.append(Image.open(io.BytesIO(inp)).convert('RGB'))
            elif hasattr(inp, 'convert'):
                images.append(inp.convert('RGB') if inp.mode != 'RGB' else inp)
            else:
                raise TypeError(f'Expected bytes or PIL Image, got {type(inp)}')
        return {'images': images, 'batch_size': len(images)}

    @staticmethod
    def inference(
        model: Any,
        preprocessed: Dict[str, Any],
        metadata: Optional[Dict] = None,
        stream: Optional[Any] = None,
        prompt: Optional[str] = None,
        max_new_tokens: Optional[int] = None,
    ) -> Any:
        """Generate a caption per image for the per-request prompt.

        Args:
            model: Loaded bundle (or an object exposing model_obj).
            preprocessed: Output of preprocess (expects 'images').
            metadata: Loader metadata (unused).
            stream: Unused streaming handle.
            prompt: Per-request text prompt; None uses the default.
            max_new_tokens: Generation budget; None uses the default.

        Returns:
            List of caption strings (one per image).
        """
        from ai.common.torch import torch

        bundle = model if isinstance(model, dict) else getattr(model, 'model_obj', model)
        mdl, processor, device = bundle['model'], bundle['processor'], bundle['device']
        dtype = bundle.get('dtype')
        prompt = prompt or DEFAULT_PROMPT
        max_new_tokens = max_new_tokens or DEFAULT_MAX_NEW_TOKENS

        # Chat template: one user turn with an image placeholder + text. This
        # exact message shape is what SmolVLM (Idefics3), Qwen3-VL and Mage-VL
        # model cards all document; each processor expands {'type': 'image'}
        # into its own vision tokens.
        messages = [
            {
                'role': 'user',
                'content': [
                    {'type': 'image'},
                    {'type': 'text', 'text': prompt},
                ],
            }
        ]
        chat_prompt = processor.apply_chat_template(messages, add_generation_prompt=True)

        captions: List[str] = []
        for image in preprocessed['images']:
            inputs = processor(text=chat_prompt, images=[image], return_tensors='pt').to(device)
            # Match pixel_values to the model dtype (bf16 on CUDA); input_ids stay long.
            if dtype is not None and 'pixel_values' in inputs:
                inputs['pixel_values'] = inputs['pixel_values'].to(dtype)
            with torch.no_grad():
                generated_ids = mdl.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    do_sample=False,  # greedy — deterministic and fast
                )
            # Decode only the newly generated tokens (strip the prompt echo).
            new_tokens = generated_ids[:, inputs['input_ids'].shape[1] :]
            text = processor.batch_decode(new_tokens, skip_special_tokens=True)[0]
            captions.append(text.strip())
        return captions

    @staticmethod
    def postprocess(
        model: Any, raw_output: Any, batch_size: int, output_fields: List[str], **kwargs
    ) -> List[Dict[str, Any]]:
        """Wrap each caption string under the 'caption' field.

        Args:
            model: Loaded bundle (unused).
            raw_output: List of caption strings.
            batch_size: Number of images (unused; arity kept for the interface).
            output_fields: Requested output fields (unused; always emits caption).
            **kwargs: Ignored extra options.

        Returns:
            List of dicts, each {'caption': str}.
        """
        return [{'caption': str(text)} for text in raw_output]


class Captioner:
    """User-facing image captioner. Model server when --modelserver is set, else local."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: Optional[str] = None,
        prompt: str = DEFAULT_PROMPT,
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        revision: Optional[str] = None,
        **kwargs,
    ):
        """Set up the captioner in proxy (model server) or local mode.

        Args:
            model_name: HF model id to load.
            device: None/'server' → model server when --modelserver is set; else a local torch device.
            prompt: Default caption prompt (per-request; not part of identity).
            max_new_tokens: Default generation budget (per-request; not part of identity).
            revision: Optional pinned model revision (part of model identity).
            **kwargs: Extra identity-only loader options forwarded to load/load_model.
        """
        self.model_name = model_name
        self.prompt = prompt or DEFAULT_PROMPT
        self.max_new_tokens = max_new_tokens or DEFAULT_MAX_NEW_TOKENS
        self._revision = revision

        server_addr = get_model_server_address()
        self._proxy_mode = bool(server_addr) and (device is None or device == 'server')

        if self._proxy_mode:
            self._client = ModelClient(server_addr)
            loader_options = {k: v for k, v in {'revision': revision, **kwargs}.items() if v is not None}
            self._client.load_model(model_name=model_name, model_type='caption', loader_options=loader_options or None)
            self._bundle = None
            self._metadata = self._client.metadata
        else:
            self._client = None
            self._bundle, self._metadata, _ = CaptionerLoader.load(
                model_name, device=device if device != 'server' else None, revision=revision, **kwargs
            )

    def caption(self, image: Any, prompt: Optional[str] = None, max_new_tokens: Optional[int] = None) -> str:
        """Return a caption string for one image.

        Args:
            image: PIL Image or encoded image bytes.
            prompt: Override the default caption prompt for this call.
            max_new_tokens: Override the default generation budget for this call.

        Returns:
            The caption as a plain string.
        """
        if image is None:
            raise ValueError('Image must not be None')

        prompt = self.prompt if prompt is None else prompt
        max_new_tokens = self.max_new_tokens if max_new_tokens is None else max_new_tokens
        metrics.counter('gpu_inference_count', 1)

        from PIL import Image
        from ai.common.image.dense_resize import resize_for_inference

        # Downscale for inference (quality-neutral; shrinks the model-server payload).
        if hasattr(image, 'size') and hasattr(image, 'mode'):
            image, _ = resize_for_inference(image, INFER_MAX_EDGE)
        elif isinstance(image, (bytes, bytearray)):
            image, _ = resize_for_inference(Image.open(io.BytesIO(image)).convert('RGB'), INFER_MAX_EDGE)

        if self._proxy_mode:
            # The model server enforces its own per-request timeout/retry.
            result = self._client.send_command(
                'rrext_ms_inference',
                {
                    'data': image_to_bytes(image),
                    'output_fields': ['caption'],
                    'prompt': prompt,
                    'max_new_tokens': max_new_tokens,
                },
            )
            items = result.get('result', [])
            return items[0].get('caption', '') if items else ''

        return self._caption_local(image, prompt, max_new_tokens)

    def _caption_local(self, image: Any, prompt: str, max_new_tokens: int) -> str:
        """Run local inference under a watchdog thread; raise TimeoutError if it hangs.

        Args:
            image: PIL Image or encoded image bytes.
            prompt: Caption prompt for this call.
            max_new_tokens: Generation budget for this call.

        Returns:
            The caption string.
        """
        result: List[Optional[str]] = [None]
        error: List[Optional[BaseException]] = [None]

        def _work():
            try:
                result[0] = self._infer_local(image, prompt, max_new_tokens)
            except BaseException as exc:  # propagated to the caller after join
                error[0] = exc

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        worker.join(timeout=INFERENCE_TIMEOUT)
        if worker.is_alive():
            raise TimeoutError(f'caption inference timed out after {INFERENCE_TIMEOUT}s')
        if error[0] is not None:
            raise error[0]
        return result[0] or ''

    def _infer_local(self, image: Any, prompt: str, max_new_tokens: int) -> str:
        """Run preprocess→inference→postprocess locally and record per-phase timing.

        Args:
            image: PIL Image or encoded image bytes.
            prompt: Caption prompt for this call.
            max_new_tokens: Generation budget for this call.

        Returns:
            The caption string.
        """
        t0 = time.perf_counter()
        pre = CaptionerLoader.preprocess(self._bundle, [image], self._metadata)
        t_pre = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        raw = CaptionerLoader.inference(self._bundle, pre, self._metadata, prompt=prompt, max_new_tokens=max_new_tokens)
        t_gpu = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        out = CaptionerLoader.postprocess(self._bundle, raw, 1, ['caption'], metadata=self._metadata)
        t_post = (time.perf_counter() - t0) * 1000
        inference_sec = (t_pre + t_gpu + t_post) / 1000.0
        metrics.add_time(
            {
                'gpu_preprocess': t_pre,
                'gpu_compute': t_gpu,
                'gpu_postprocess': t_post,
                'gpu_queue_wait': 0,
                'gpu_memory': model_gpu_gb(self._bundle) * inference_sec,
            }
        )
        return out[0]['caption']

    def disconnect(self) -> None:
        """Release the model-server connection (proxy mode only); no-op locally.

        Returns:
            None.
        """
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
