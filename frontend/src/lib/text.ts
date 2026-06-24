// Defense-in-depth: descriptions are sanitized at ingest (backend @validates + backfill), but a
// future un-cleaned adapter could still slip raw HTML/entities into a description. The UI renders
// descriptions as PLAIN TEXT (whitespace-pre-line), so strip tags + unescape common entities at
// display too. Fast-path returns clean text untouched (preserves newlines exactly).
const ENTITIES: Record<string, string> = {
  "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&#x27;": "'", "&nbsp;": " ",
};

export function cleanText(s: string | null | undefined): string {
  if (!s) return "";
  if (!/[<&]/.test(s)) return s; // no markup → return as-is (keeps paragraph breaks intact)
  return s
    .replace(/<[^>]+>/g, " ")
    .replace(/&[a-z#0-9]+;/gi, (m) => ENTITIES[m.toLowerCase()] ?? m)
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}
