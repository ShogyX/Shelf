// A small language badge for a title card — shown ONLY for non-English titles (English is the
// common case, so tagging every "EN" card would just add noise). The DTO's `language` is an ISO
// code (canonical-ish, may be null); null / "en" / "eng" / "en-*" all count as English → no badge.
// The code is rendered as a short uppercase tag ("NO", "JA") via the `languages.<code>` i18n map,
// falling back to the uppercased code for anything we haven't named.
import { useTranslation } from "react-i18next";
import { Badge } from "./ui";

/** True for a language we treat as English (→ render no badge). Guards on null and en/eng/en-* tags. */
export function isEnglishLang(code: string | null | undefined): boolean {
  const l = (code ?? "").trim().toLowerCase();
  return l === "" || l === "en" || l === "eng" || l.startsWith("en-");
}

/** The short language display name for a code ("Norsk", "Japansk"), via `languages.<code>` with an
 *  uppercased-code fallback. Exported so filters/menus can label a raw code the same way. */
export function useLanguageName() {
  const { t } = useTranslation();
  return (code: string) => t(`languages.${code.toLowerCase()}`, code.toUpperCase());
}

/** Renders a language badge for non-English titles; renders nothing for English (or missing) codes. */
export function LanguageBadge({ language }: { language: string | null | undefined }) {
  const languageName = useLanguageName();
  if (isEnglishLang(language)) return null;
  const code = language!.trim();
  return (
    <span title={languageName(code)}>
      <Badge>{code.split("-")[0].toUpperCase()}</Badge>
    </span>
  );
}
