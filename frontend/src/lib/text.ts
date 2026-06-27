// Defense-in-depth: descriptions are sanitized at ingest (backend @validates + backfill), but a
// future un-cleaned adapter could still slip raw HTML/entities into a description. The UI renders
// descriptions as PLAIN TEXT (whitespace-pre-line), so strip tags + unescape common entities at
// display too. Fast-path returns clean text untouched (preserves newlines exactly).
const ENTITIES: Record<string, string> = {
  "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&#x27;": "'", "&nbsp;": " ",
};

// Comic/PDF imports sometimes store the raw release FILENAME as the title, e.g.
// "Dynamite-Robert.Jordan.s.Wheel.Of.Time.No.32.2013.Comic.eBook-BitBook". A human title always has
// spaces, so leave those untouched; only a space-less, dot/underscore-delimited string is treated as
// a filename and made readable (drop a trailing file extension, turn separators into spaces, collapse).
export function cleanTitle(raw: string | null | undefined): string {
  const s = (raw ?? "").trim();
  if (!s || /\s/.test(s) || !/[._]/.test(s)) return s;
  return (
    s
      .replace(/\.(pdf|epub|cbz|cbr|cb7|mobi|azw3?|txt|fb2|djvu)$/i, "")
      .replace(/[._]+/g, " ")
      .replace(/\s{2,}/g, " ")
      .trim() || s
  );
}

export function cleanText(s: string | null | undefined): string {
  if (!s) return "";
  if (!/[<&]/.test(s)) return s; // no markup → return as-is (keeps paragraph breaks intact)
  return s
    .replace(/<[^>]+>/g, " ")
    .replace(/&[a-z#0-9]+;/gi, (m) => ENTITIES[m.toLowerCase()] ?? m)
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}
