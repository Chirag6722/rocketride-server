"""Unit tests for the segmentation loader + facade (no torch/transformers needed)."""

import ai.common.models.vision.segmentation as segmod
from ai.common.models.vision.segmentation import SegmenterLoader, Segmenter, Sam3ConceptLoader

INSTANCE_MODEL = 'facebook/mask2former-swin-tiny-coco-instance'
SEMANTIC_MODEL = 'facebook/mask2former-swin-tiny-ade-semantic'
SAM3_MODEL = 'facebook/sam3'


def test_postprocess_wraps_masks():
    inst = [{'label': 'person', 'score': 0.9}]
    sem = {'semantic_map': {'size': [2, 2], 'counts': 'x'}, 'classes': {1: 'wall'}}
    out = SegmenterLoader.postprocess(None, [inst, sem], 2, ['masks'])
    assert out == [
        {'masks': inst, '$masks': inst},
        {'masks': sem, '$masks': sem},
    ]


def test_model_id_mode_is_identity():
    inst = SegmenterLoader.generate_model_id(INSTANCE_MODEL, mode='instance')
    assert inst == SegmenterLoader.generate_model_id(INSTANCE_MODEL, mode='instance')
    # Different mode -> different model identity (separate server copies).
    assert SegmenterLoader.generate_model_id(SEMANTIC_MODEL, mode='semantic') != inst


def _fake_client_factory(captured):
    class FakeClient:
        def __init__(self, addr):
            self.metadata = {}

        def load_model(self, model_name=None, model_type=None, loader_options=None):
            captured.setdefault('loads', []).append((model_name, model_type, loader_options))

        def send_command(self, command, args):
            captured['cmd'] = command
            captured['args'] = args
            return {'result': [{'masks': captured.get('masks', [])}]}

        def disconnect(self):
            captured['disconnected'] = True

    return FakeClient


def test_facade_load_once_ignores_threshold_and_maxedge(monkeypatch):
    captured = {}
    monkeypatch.setattr(segmod, 'get_model_server_address', lambda: 'localhost:5590')
    monkeypatch.setattr(segmod, 'ModelClient', _fake_client_factory(captured))

    Segmenter(mode='instance', threshold=0.1, max_edge=512)
    Segmenter(mode='instance', threshold=0.9, max_edge=2048)

    loads = captured['loads']
    assert loads[0] == loads[1]  # same identity regardless of per-request threshold / client-side max_edge
    assert loads[0][1] == 'segmentation'
    opts = loads[0][2] or {}
    assert 'threshold' not in opts and 'max_edge' not in opts
    assert opts.get('mode') == 'instance'


# ---------------------------------------------------------------------------
# SAM 3 concept backend (mocked model/processor — no weight downloads)
# ---------------------------------------------------------------------------


class _FakeTensor:
    """Minimal tensor stand-in exercising the .detach().cpu() guard path."""

    def __init__(self, data):
        self._data = data

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        import numpy as np

        return np.asarray(self._data)

    def tolist(self):
        return list(self._data)


class _FakeOutputs(dict):
    def __init__(self, **kwargs):
        super().__init__(kwargs)


class _FakeNoGradTorch:
    class no_grad:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False


class _FakeBatch(dict):
    def to(self, device):
        return self


def _make_sam3_backend(results, captured, threshold=0.5):
    """Assemble a Sam3ConceptLoader without running __init__ (no transformers)."""

    class FakeProcessor:
        def __call__(self, images=None, text=None, return_tensors=None):
            captured['text'] = text
            return _FakeBatch(pixel_values=_FakeTensor([0]))

        def post_process_instance_segmentation(self, outputs, threshold=None, mask_threshold=None, target_sizes=None):
            captured['pp_threshold'] = threshold
            captured['pp_mask_threshold'] = mask_threshold
            captured['pp_target_sizes'] = target_sizes
            return [results]

    backend = Sam3ConceptLoader.__new__(Sam3ConceptLoader)
    backend.model_name = SAM3_MODEL
    backend.threshold = threshold
    backend.device = 'cpu'
    backend._processor = FakeProcessor()
    backend._model = lambda **kwargs: _FakeOutputs(pred_masks=_FakeTensor([0]))
    backend._torch = _FakeNoGradTorch()
    return backend


def test_sam3_empty_prompt_returns_empty_without_inference():
    captured = {}
    backend = _make_sam3_backend({}, captured)

    from PIL import Image

    img = Image.new('RGB', (8, 8))
    assert backend.segment(img, prompt='') == []
    assert backend.segment(img, prompt='   ') == []
    assert backend.segment(img) == []
    assert 'text' not in captured  # processor never invoked


def test_sam3_output_contract(monkeypatch):
    """Output-shape contract: [{label, score, box{x1,y1,x2,y2}, mask(RLE)}], threshold-filtered."""
    import numpy as np
    from PIL import Image

    monkeypatch.setattr(segmod, '_encode_rle', lambda m: {'size': list(np.asarray(m).shape), 'counts': 'stub'})

    keep = np.zeros((8, 8), dtype=np.uint8)
    keep[2:5, 3:6] = 1
    low_score = np.ones((8, 8), dtype=np.uint8)
    empty = np.zeros((8, 8), dtype=np.uint8)

    captured = {}
    results = {
        'masks': [_FakeTensor(keep), _FakeTensor(low_score), _FakeTensor(empty)],
        'boxes': [_FakeTensor([3.0, 2.0, 6.0, 5.0]), _FakeTensor([0, 0, 8, 8]), _FakeTensor([0, 0, 0, 0])],
        'scores': [_FakeTensor(0.91).numpy(), 0.2, 0.9],  # plain floats/ndarray both fine
    }
    backend = _make_sam3_backend(results, captured, threshold=0.5)

    out = backend.segment(Image.new('RGB', (8, 8)), prompt='yellow school bus')

    assert captured['text'] == 'yellow school bus'
    assert captured['pp_threshold'] == 0.5
    assert captured['pp_mask_threshold'] == Sam3ConceptLoader.MASK_THRESHOLD
    assert captured['pp_target_sizes'] == [(8, 8)]

    # low-score instance filtered, all-empty mask skipped -> exactly one instance
    assert len(out) == 1
    inst = out[0]
    assert set(inst) == {'label', 'score', 'box', 'mask'}
    assert inst['label'] == 'yellow school bus'
    assert inst['score'] == 0.91
    assert inst['box'] == {'x1': 3.0, 'y1': 2.0, 'x2': 6.0, 'y2': 5.0}
    assert inst['mask'] == {'size': [8, 8], 'counts': 'stub'}


def test_sam3_prompt_list_splits_into_one_query_per_concept(monkeypatch):
    """' . '-separated prompts (the detect node convention) fan out to one PCS
    query per concept, each instance labelled with the concept it matched.
    """
    import numpy as np
    from PIL import Image

    monkeypatch.setattr(segmod, '_encode_rle', lambda m: {'size': list(np.asarray(m).shape), 'counts': 'stub'})

    mask = np.ones((4, 4), dtype=np.uint8)
    captured = {}
    queries = []
    results = {'masks': [_FakeTensor(mask)], 'boxes': [_FakeTensor([0, 0, 4, 4])], 'scores': [0.9]}
    backend = _make_sam3_backend(results, captured)

    class RecordingProcessor:
        def __init__(self, inner):
            self._inner = inner

        def __call__(self, images=None, text=None, return_tensors=None):
            queries.append(text)
            return self._inner(images=images, text=text, return_tensors=return_tensors)

        def post_process_instance_segmentation(self, *args, **kwargs):
            return self._inner.post_process_instance_segmentation(*args, **kwargs)

    backend._processor = RecordingProcessor(backend._processor)

    out = backend.segment(Image.new('RGB', (4, 4)), prompt='grass . tree .  stairs ')
    assert queries == ['grass', 'tree', 'stairs']
    assert [inst['label'] for inst in out] == ['grass', 'tree', 'stairs']

    queries.clear()
    assert backend.segment(Image.new('RGB', (4, 4)), prompt=' . . ') == []
    assert queries == []  # separators only: no inference


def test_sam3_threshold_override(monkeypatch):
    import numpy as np
    from PIL import Image

    monkeypatch.setattr(segmod, '_encode_rle', lambda m: {'size': list(np.asarray(m).shape), 'counts': 'stub'})

    mask = np.ones((4, 4), dtype=np.uint8)
    captured = {}
    results = {'masks': [_FakeTensor(mask)], 'boxes': [_FakeTensor([0, 0, 4, 4])], 'scores': [0.4]}
    backend = _make_sam3_backend(results, captured, threshold=0.5)

    assert backend.segment(Image.new('RGB', (4, 4)), prompt='cat') == []  # 0.4 < default 0.5
    out = backend.segment(Image.new('RGB', (4, 4)), prompt='cat', threshold=0.3)
    assert len(out) == 1 and captured['pp_threshold'] == 0.3


def test_sam3_model_id_mode_is_identity():
    sam3 = SegmenterLoader.generate_model_id(SAM3_MODEL, mode='sam3')
    assert sam3 == SegmenterLoader.generate_model_id(SAM3_MODEL, mode='sam3')
    assert sam3 != SegmenterLoader.generate_model_id(INSTANCE_MODEL, mode='instance')


def test_loader_inference_passes_prompt():
    calls = []

    class FakeBackend:
        def segment(self, img, prompt=None, threshold=None):
            calls.append((img, prompt, threshold))
            return []

    bundle = {'segmenter': FakeBackend(), 'mode': 'sam3'}
    out = SegmenterLoader.inference(bundle, {'images': ['img1']}, prompt='cat', threshold=0.4)
    assert calls == [('img1', 'cat', 0.4)]
    assert out == [[]]


def test_facade_sam3_prompt_is_per_request_not_identity(monkeypatch):
    from PIL import Image

    captured = {'masks': []}
    monkeypatch.setattr(segmod, 'get_model_server_address', lambda: 'localhost:5590')
    monkeypatch.setattr(segmod, 'ModelClient', _fake_client_factory(captured))

    seg = Segmenter(mode='sam3', prompt='yellow school bus', threshold=0.5)
    opts = captured['loads'][0][2] or {}
    assert opts.get('mode') == 'sam3'
    assert 'prompt' not in opts  # per-request, not part of model identity

    out = seg.segment(Image.new('RGB', (8, 8)))
    assert captured['cmd'] == 'rrext_ms_inference'
    assert captured['args']['prompt'] == 'yellow school bus'
    assert out == []

    # Per-call override wins over the constructor default.
    seg.segment(Image.new('RGB', (8, 8)), prompt='red kayak')
    assert captured['args']['prompt'] == 'red kayak'
