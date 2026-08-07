---
title: 'drizzle (rocketride/drizzle)'
date: 2026-08-07
---

# Drizzle ORM over pipes

- [Overview](#overview)
- [Method Signature](#method-signature)
- [Parameters](#parameters)
- [Prerequisites](#prerequisites)
- [Examples](#examples)
  - [Define a schema and query rows](#define-a-schema-and-query-rows)
  - [Transactions](#transactions)
  - [Nested transactions and isolation config](#nested-transactions-and-isolation-config)
- [Related Methods](#related-methods)

## Overview

The `rocketride/drizzle` entry point builds a [Drizzle ORM](https://orm.drizzle.team/) instance whose Postgres driver transports SQL over a RocketRide pipeline instead of a TCP socket. No database connection is opened by the client — every query is forwarded through the pipeline's `execute` tool function, and transactions ride the `begin`/`commit`/`rollback` tool functions with full support for isolation config and nested savepoints.

Because `drizzle-orm` has zero runtime dependencies and no Node built-ins, this integration is browser-bundle safe — no polyfills required. It also lives in its own package export, so apps that never use the ORM pay nothing for it.

> **Requirement:** the target database node must have `allow_execute: true` set in its pipeline configuration. The same flag also gates transactions (`begin`/`commit`/`rollback`) — no additional configuration is needed.

## Method Signature

```typescript
import { drizzle } from 'rocketride/drizzle';

drizzle(options: {
  client: DatabaseLike;      // pass client.database
  token: string;
  nodeId?: string;
} & DrizzleConfig): PgDatabase;
```

## Parameters

| Parameter | Type           | Required | Description                                                                                  |
| --------- | -------------- | -------- | -------------------------------------------------------------------------------------------- |
| `client`  | `DatabaseLike` | Yes      | The SDK transport — pass `client.database`.                                                  |
| `token`   | `string`       | Yes      | Pipeline token for authentication and resource access.                                       |
| `nodeId`  | `string`       | No       | Target database node id; pins queries and transactions to one node.                          |
| `schema`  | `DrizzleConfig['schema']` | No | Drizzle schema object enabling the relational query API (`db.query.*`).            |
| `logger`  | `boolean \| Logger` | No  | `true` for Drizzle's `DefaultLogger`, or a custom `Logger`.                                  |
| `casing`  | `DrizzleConfig['casing']` | No | Column-name casing convention passed through to the Drizzle dialect.               |

## Prerequisites

1. A running pipeline with a Postgres database node (`allow_execute: true`).
2. `drizzle-orm` installed as an optional peer dependency: `npm install drizzle-orm` (supported range: `0.45.x`).
3. **Tables must already exist in the target database** — drizzle-kit schema management (`push`, `studio`) is not part of this integration; migrations run from a trusted context via `client.database.query()` if needed.

## Examples

### Define a schema and query rows

```typescript
import { RocketRideClient } from 'rocketride';
import { drizzle } from 'rocketride/drizzle';
import { eq } from 'drizzle-orm';
import { integer, pgTable, text } from 'drizzle-orm/pg-core';

const users = pgTable('users', {
	id: integer('id').primaryKey(),
	name: text('name'),
	email: text('email'),
});

const client = new RocketRideClient({
	auth: process.env.ROCKETRIDE_APIKEY!,
	uri: 'wss://cloud.rocketride.ai',
});
await client.connect();

const { token } = await client.use({ filepath: './db-pipeline.pipe' });

// Build a Drizzle instance backed by the RocketRide pipeline
const db = drizzle({ client: client.database, token, nodeId: 'my-postgres-node' });

// Fully typed queries — table must exist in the target DB
const activeUsers = await db.select().from(users).where(eq(users.id, 1));
console.log(activeUsers);

await client.terminate(token);
await client.disconnect();
```

### Transactions

Transactions are forwarded through the pipeline's `begin`/`commit`/`rollback` tool functions. Use `db.transaction()` exactly as you would with any Drizzle driver — if the callback throws, the transaction is automatically rolled back:

```typescript
const db = drizzle({ client: client.database, token, nodeId: 'my-postgres-node' });

await db.transaction(async (tx) => {
	await tx.update(accounts).set({ balance: sql`${accounts.balance} - 100` }).where(eq(accounts.id, 1));
	await tx.update(accounts).set({ balance: sql`${accounts.balance} + 100` }).where(eq(accounts.id, 2));
	// Throwing here rolls the whole transaction back.
});
```

### Nested transactions and isolation config

Nested `tx.transaction()` calls map to Postgres savepoints; the optional config maps to `SET TRANSACTION`:

```typescript
await db.transaction(
	async (tx) => {
		await tx.insert(orders).values({ id: 1 });
		try {
			await tx.transaction(async (tx2) => {
				await tx2.insert(auditLog).values({ orderId: 1 });
				throw new Error('audit failed'); // rolls back the savepoint only
			});
		} catch {
			// outer transaction continues and commits
		}
	},
	{ isolationLevel: 'serializable' }
);
```

## Related Methods

- `database.query()` — raw SQL over the pipeline; supports `rowMode: 'array'` for positional rows.
- `database.beginTransaction()` / `database.commit()` / `database.rollback()` — the session primitives the driver rides on.
- `database.dialect()` — discover the underlying engine of a node.
