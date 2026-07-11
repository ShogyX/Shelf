// Browse all audiobooks — the full grid of the shared audio pool (the Discover "Audiobooks" rail's
// "see all" target, #10). Clicking a cover starts playback in the global player.
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { CoverCard } from "../components/CoverCard";
import { EmptyState, PosterGridSkeleton } from "../components/ui";
import { Headphones } from "lucide-react";

export default function BrowseAudiobooks() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // Same query key as the Discover rail so navigating here is instant (cache-warm).
  const q = useQuery({ queryKey: ["catalog-audiobooks", "all"], queryFn: () => api.catalogAudiobooks(10000) });
  const items = q.data ?? [];
  return (
    <div className="mx-auto max-w-6xl px-4 pb-10 pt-8 sm:px-6">
      <h1 className="mb-1 font-display text-[34px] font-semibold leading-[1.05] tracking-tight text-text sm:text-[44px]">
        {t("discover.audiobooks")}
      </h1>
      <p className="mb-5 text-sm text-muted">{t("audiobooks.subtitle", { count: items.length })}</p>
      {q.isLoading ? (
        <PosterGridSkeleton />
      ) : items.length === 0 ? (
        <EmptyState
 icon={<Headphones className="h-7 w-7" />}
 title={t("audiobooks.emptyTitle")} hint={t("audiobooks.emptyHint")} />
      ) : (
        <div className="flex flex-wrap gap-[18px]">
          {items.map((a) => (
            <CoverCard
              key={a.work_id}
              title={a.title}
              author={a.author}
              coverUrl={a.cover_url}
              kind="audio"
              subtitle={a.author ?? undefined}
              onClick={() => navigate(`/discover?q=${encodeURIComponent(a.title)}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
