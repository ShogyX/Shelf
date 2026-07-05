// Top-level Issues route: flagged-title reports. IssuesPanel already scopes to the caller's own
// issues (or everyone's, for admins / issues.view_all holders) and carries the admin resolve/reopen
// + management actions, so this page is a thin titled container around it.
import { useTranslation } from "react-i18next";
import IssuesPanel from "../components/IssuesPanel";

export default function Issues() {
  const { t } = useTranslation();
  return (
    <div className="page-in mx-auto max-w-4xl px-4 sm:px-6 py-6">
      <h1 className="font-display mb-4 text-2xl font-semibold tracking-tight text-text">{t("nav.issues")}</h1>
      <IssuesPanel />
    </div>
  );
}
