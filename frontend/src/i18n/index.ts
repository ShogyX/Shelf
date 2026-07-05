// i18next setup for the Shelf UI. Two bundles ship in the app (en / no); English is the fallback for
// any missing key. React already escapes interpolated values, so i18next's own escaping is disabled.
//
// Language resolution order (LanguageDetector):
//   1. an explicit set (i18n.changeLanguage — driven by the signed-in user's `locale`)
//   2. localStorage `shelf_locale` (so a logged-out / first paint matches the last choice)
//   3. the browser's navigator.language (nb/nn/no* → "no", everything else → "en")
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import en from "./en.json";
import no from "./no.json";

export const SUPPORTED_LOCALES = ["en", "no"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];
export const LOCALE_STORAGE_KEY = "shelf_locale";

// Map a raw browser language tag to one of our two supported codes. Norwegian has several tags
// (nb = bokmål, nn = nynorsk, no = macro) — all collapse to "no"; anything else falls back to "en".
export function normalizeLocale(tag: string | null | undefined): Locale {
  const l = (tag ?? "").toLowerCase();
  if (l === "no" || l.startsWith("nb") || l.startsWith("nn") || l.startsWith("no-")) return "no";
  return "en";
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      no: { translation: no },
    },
    fallbackLng: "en",
    supportedLngs: SUPPORTED_LOCALES as unknown as string[],
    // A raw browser tag (e.g. "nb-NO") is resolved to "no" via our normalizer, not by loading a
    // "nb-NO" bundle — so keep only the base code and let the custom detector do the mapping.
    load: "languageOnly",
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: LOCALE_STORAGE_KEY,
      caches: ["localStorage"],
      // Fold any Norwegian browser tag onto "no"; everything else onto "en".
      convertDetectedLanguage: (lng: string) => normalizeLocale(lng),
    },
    interpolation: { escapeValue: false }, // React escapes for us
    returnNull: false,
  });

/** Switch the UI language and remember it (so the next cold start paints in the same language). */
export function setLocale(code: string) {
  const locale = normalizeLocale(code);
  localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  i18n.changeLanguage(locale);
}

export default i18n;
