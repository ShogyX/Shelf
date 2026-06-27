// Centralized TanStack Query key factory (`qk`).
//
// Each function returns the EXACT array previously written inline as a `queryKey` / the target of an
// `invalidateQueries` / `setQueryData`. Co-locating them keeps a query and its invalidations in sync:
// they must produce identical arrays (same elements, same order) or caching/invalidation silently
// breaks — and TypeScript cannot catch a divergence between two arrays.
//
// Variadic keys (works, missing, indexPages, catalogCategories) mirror the existing pattern where a
// useQuery uses a fully-specified key and an invalidateQueries uses the bare namespace prefix
// (TanStack matches by prefix): call with no args for the prefix form, with args for the full key.
//
// Intentionally NOT migrated (left as literals at their call sites) — the param-laden infinite-query
// keys, where matching every site's exact argument order is risky and a silent cache break can't be
// type-checked:
//   • Index.tsx        ["catalog", debounced, live, mediaFilter, sourceFilter, sortBy]
//   • BrowseCatalog.tsx ["catalog-browse", dimension, value, media, sort]
// (the bare ["catalog"] invalidation prefix IS provided here as qk.catalog(), which prefix-matches
//  the Index infinite query.)

export const qk = {
  // --- works & reading ---
  works: (q?: string, shelfId?: number | null) =>
    q === undefined ? (["works"] as const) : (["works", q, shelfId] as const),
  // id is widened to number | null because CatalogCard keys ["work", group.hooked_work_id] where
  // hooked_work_id may be null (the query is disabled in that case); the array stays byte-identical.
  work: (id: number | null) => ["work", id] as const,
  // id widened to number | undefined: Library keys ["work-series", seedId] where seedId (books[0]?.id)
  // may be undefined (the query is disabled then); the array stays byte-identical.
  workSeries: (id: number | undefined) => ["work-series", id] as const,
  workMetadata: (id: number) => ["work-metadata", id] as const,
  workRelated: (id: number) => ["work-related", id] as const,
  chaptersAll: (id: number) => ["chapters-all", id] as const,
  // id widened to number | undefined: Reader keys ["chapter", resolvedChapterId] where the id may be
  // undefined (the query is disabled then); the array stays byte-identical to the former literal.
  chapter: (id: number | undefined) => ["chapter", id] as const,
  progress: (id: number) => ["progress", id] as const,
  continue: () => ["continue"] as const,
  continueListening: () => ["continue-listening"] as const,
  queuedHooks: () => ["queued-hooks"] as const,

  // --- bookshelves ---
  bookshelves: () => ["bookshelves"] as const,

  // --- catalog & discovery ---
  // ["catalog"] is the bare invalidation prefix; the Index infinite query stays a literal.
  catalog: () => ["catalog"] as const,
  catalogStats: () => ["catalog-stats"] as const,
  catalogFacets: () => ["catalog-facets"] as const,
  catalogRows: () => ["catalog-rows"] as const,
  catalogCategories: (media?: string) =>
    media === undefined ? (["catalog-categories"] as const) : (["catalog-categories", media] as const),
  series: (catalogId: number) => ["series", catalogId] as const,
  author: (catalogId: number) => ["author", catalogId] as const,
  indexSearch: (q: string) => ["index-search", q] as const,
  indexLayout: () => ["index-layout"] as const,
  indexStats: () => ["index-stats"] as const,
  indexSites: () => ["index-sites"] as const,
  indexPages: (siteId?: number) =>
    siteId === undefined ? (["index-pages"] as const) : (["index-pages", siteId] as const),
  indexPage: (id: number) => ["index-page", id] as const,
  indexBlocks: () => ["index-blocks"] as const,
  indexConfig: () => ["index-config"] as const,
  bookCatalog: () => ["book-catalog"] as const,
  featuredConfig: () => ["featured-config"] as const,

  // --- sources & jobs ---
  sources: () => ["sources"] as const,
  adapters: () => ["adapters"] as const,
  jobs: () => ["jobs"] as const,

  // --- watched local folders ---
  folders: () => ["folders"] as const,

  // --- downloads & acquisition ---
  downloads: () => ["downloads"] as const,
  fetchPriority: () => ["fetch-priority"] as const,

  // --- stocking ---
  stockSummary: () => ["stock-summary"] as const,
  stockJobs: () => ["stock-jobs"] as const,
  stockJob: (id: number) => ["stock-job", id] as const,

  // --- integrations ---
  integrations: () => ["integrations"] as const,
  integrationCatalog: () => ["integration-catalog"] as const,
  metadataStats: () => ["metadata-stats"] as const,

  // --- notifications ---
  notifications: () => ["notifications"] as const,
  notifUnread: () => ["notif-unread"] as const,
  notifChannels: () => ["notif-channels"] as const,
  notifPrefs: () => ["notif-prefs"] as const,
  notifGlobalChannel: () => ["notif-global-channel"] as const,
  notifAdminPrefs: () => ["notif-admin-prefs"] as const,

  // --- settings & system ---
  settings: () => ["settings"] as const,
  storage: () => ["storage"] as const,
  systemConfig: () => ["system-config"] as const,
  globalSmtp: () => ["global-smtp"] as const,
  crawlTuning: () => ["crawl-tuning"] as const,
  operatorIdentity: () => ["operator-identity"] as const,
  myGoodreads: () => ["my-goodreads"] as const,
  requestStats: (hours: number) => ["request-stats", hours] as const,
  pipelineStats: () => ["pipeline-stats"] as const,
  statsAcquisitions: (days: number) => ["stats-acquisitions", days] as const,
  statsLibraryGrowth: (days: number) => ["stats-library-growth", days] as const,
  statsOverview: () => ["stats-overview"] as const,
  statsVtUsage: () => ["stats-vt-usage"] as const,
  backups: () => ["backups"] as const,
  restorePlan: (name: string) => ["restore-plan", name] as const,

  // --- missing-content ledger ---
  missing: (status?: string, reason?: string, sort?: string) =>
    status === undefined && reason === undefined && sort === undefined
      ? (["missing"] as const)
      : (["missing", status, reason, sort] as const),
  missingStats: () => ["missing-stats"] as const,
  rescanStatus: () => ["rescan-status"] as const,

  // --- following (subscriptions) ---
  subscriptions: () => ["subscriptions"] as const,

  // --- external reading-list imports ---
  listImports: () => ["list-imports"] as const,
  listImportProviders: () => ["list-import-providers"] as const,
  listImportItems: (id: number) => ["list-import-items", id] as const,

  // --- users & permissions ---
  users: () => ["users"] as const,
  categoryDefault: () => ["category-default"] as const,
  adultAllowed: () => ["adult-allowed"] as const,
  permissionsMeta: () => ["permissions-meta"] as const,
};
