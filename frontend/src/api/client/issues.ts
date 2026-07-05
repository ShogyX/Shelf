// Issues (flagging): a user flags a title with a problem; admins triage + resolve. A user always
// sees issues they raised; an admin — or a user granted `issues.view_all` — sees everyone's.
import { req } from "./http";

export type IssueKind = "no_content" | "wrong_metadata" | "broken_file" | "wrong_language" | "other";

export interface Issue {
  id: number;
  work_id: number | null;
  user_id: number | null;
  username: string | null;          // reporter (visible to admin / view_all holders / the reporter)
  title: string;
  kind: IssueKind | string;
  description: string;
  status: "open" | "resolved";
  admin_note: string | null;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
  mine: boolean;                    // the caller raised this
  can_resolve: boolean;            // the caller (admin) may resolve/edit it
}

export const ISSUE_KINDS: IssueKind[] = [
  "no_content", "wrong_metadata", "broken_file", "wrong_language", "other",
];

export const issuesApi = {
  listIssues: (params: { status?: string; scope?: string } = {}) => {
    const p = new URLSearchParams();
    if (params.status) p.set("status", params.status);
    if (params.scope) p.set("scope", params.scope);
    const qs = p.toString();
    return req<Issue[]>(`/issues${qs ? `?${qs}` : ""}`);
  },
  issuesCount: () => req<{ open: number; view_all: boolean }>("/issues/count"),
  createIssue: (body: { work_id?: number | null; kind: string; description: string }) =>
    req<Issue>("/issues", { method: "POST", body: JSON.stringify(body) }),
  updateIssue: (id: number, body: { status?: string; admin_note?: string }) =>
    req<Issue>(`/issues/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteIssue: (id: number) => req<{ deleted: number }>(`/issues/${id}`, { method: "DELETE" }),
};
