# Max portal query recovery

- INC, bulk INC, and name lookups now use the Max request query observed in the portal instead of unsupported filters.
- Keep available matches and flag an incomplete search if Max fails while loading later pages.
