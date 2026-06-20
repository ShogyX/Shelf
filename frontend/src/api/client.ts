// Thin typed REST client for the Shelf API.
//
// This file is a BARREL: the implementation is split by domain under ./client/*, and everything is
// re-exported here so existing imports (`import { api, Foo } from "../api/client"`) keep working
// unchanged. The single `api` object is assembled from the per-domain slices below — its methods,
// names and signatures are identical to before the split.

export { ApiError } from "./client/http";

export * from "./client/works";
export * from "./client/bookshelves";
export * from "./client/sources";
export * from "./client/integrations";
export * from "./client/notifications";
export * from "./client/catalog";
export * from "./client/downloads";
export * from "./client/stock";
export * from "./client/users";
export * from "./client/system";
export * from "./client/folders";
export * from "./client/subscriptions";

import { worksApi } from "./client/works";
import { bookshelvesApi } from "./client/bookshelves";
import { sourcesApi } from "./client/sources";
import { integrationsApi } from "./client/integrations";
import { notificationsApi } from "./client/notifications";
import { catalogApi } from "./client/catalog";
import { downloadsApi } from "./client/downloads";
import { stockApi } from "./client/stock";
import { usersApi } from "./client/users";
import { systemApi } from "./client/system";
import { foldersApi } from "./client/folders";
import { subscriptionsApi } from "./client/subscriptions";

export const api = {
  ...systemApi,
  ...worksApi,
  ...bookshelvesApi,
  ...sourcesApi,
  ...notificationsApi,
  ...catalogApi,
  ...stockApi,
  ...integrationsApi,
  ...downloadsApi,
  ...foldersApi,
  ...usersApi,
  ...subscriptionsApi,
};
