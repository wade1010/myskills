# React and Next.js audit reference

Use these as investigation signals, not automatic findings.

## Data reachability

- Trace list controls from component state or URL params through the query key, tRPC/route input, Prisma/datastore query, and response metadata.
- Look for fixed `take`, `limit`, `slice`, first-page defaults, or capped queries followed by client-side filter/sort/pagination. Prove the effective order of operations before filing a gap.
- Check whether filters reset or invalidate the current page and whether deletion can leave the user stranded on an empty page.
- A rendered row count does not prove the user can reach the complete authorized dataset.

## Lifecycle and mutations

- Trace whether users can actually enter and complete applicable create, view, update, archive/delete, and recovery paths. A server procedure without a reachable UI path is not automatically covered.
- Verify authorization inside server procedures or route handlers and record scoping predicates. Hidden buttons are not enforcement.
- Inspect validation mapping, duplicate-submit protection, mutation errors, cache invalidation, optimistic rollback, and post-success navigation.
- Distinguish initial empty, filtered no-results, loading, refetching, failure, and forbidden states. Watch for error handlers that return empty arrays or otherwise turn failures into empty states.

## UI and accessibility

- Treat shadcn/ui and TanStack imports as implementation clues, not evidence of focus management, labels, keyboard behavior, table semantics, or responsiveness.
- Check whether relation selectors expose useful labels rather than requiring raw IDs.
- Source can prove accessible names and structural relationships in some cases. Focus return, dialog containment, narrow-screen usability, visual clipping, and announcement timing usually require a browser or UI test; otherwise use `manual-review`.
