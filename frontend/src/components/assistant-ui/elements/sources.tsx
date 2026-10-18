"use client";

import { ChevronDownIcon } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { collapsePanel, mono } from "./surfaces";
import { hostOf, safeHref } from "../utils/href";

export interface Source {
  domain?: string | undefined;
  title: string;
  url?: string | undefined;
  snippet?: string | undefined;
  author?: string | undefined;
  publishedAt?: string | undefined;
  location?: string | undefined;
  identity?: string | undefined;
  sourceId?: string | undefined;
}

export interface SourcesProps {
  sources: readonly Source[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  locale?: string | undefined;
  className?: string | undefined;
}

const displayDomain = (source: Source) => source.domain || hostOf(source.url);

const formatPublishedAt = (publishedAt: string | undefined, locale: string) => {
  if (!publishedAt) return undefined;
  const date = new Date(publishedAt);
  if (Number.isNaN(date.valueOf())) return undefined;
  const options: Intl.DateTimeFormatOptions = {
    month: "short",
    year: "numeric",
  };
  try {
    return new Intl.DateTimeFormat(locale, options).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-US", options).format(date);
  }
};

export function Sources({
  sources,
  open,
  onOpenChange,
  locale = "en-US",
  className,
}: SourcesProps) {
  const badgeDomains = sources
    .map(displayDomain)
    .filter((domain): domain is string => domain !== undefined)
    .slice(0, 3);

  return (
    <Collapsible
      data-slot="sources"
      open={open}
      onOpenChange={onOpenChange}
      className={cn("w-full", className)}
    >
      <CollapsibleTrigger
        className={cn(
          "group/trigger text-foreground/60 hover:text-foreground/90 inline-flex w-fit items-center gap-1.5 py-1 text-xs outline-none",
        )}
      >
        {badgeDomains.length > 0 ? (
          <span aria-hidden className="flex -space-x-1">
            {badgeDomains.map((domain, index) => (
              <span
                key={`${domain}-${index}`}
                data-slot="sources-badge"
                className="bg-foreground/[0.08] text-foreground/55 ring-background dark:ring-popover flex size-4 items-center justify-center rounded-full text-[8px] font-medium ring-1"
              >
                {domain.charAt(0).toUpperCase()}
              </span>
            ))}
          </span>
        ) : null}
        <span>查看依据</span>
        <span className={cn(mono, "text-foreground/35 tabular-nums")}>
          {sources.length}
        </span>
        <ChevronDownIcon className="size-3 opacity-60 group-data-open/trigger:rotate-180 group-data-panel-open/trigger:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent className={cn(collapsePanel, "outline-none")}>
        <div className="grid grid-cols-1 gap-2 pt-2.5">
          {sources.map((source, index) => {
            const domain = displayDomain(source);
            const href = safeHref(source.url);
            const meta = [
              source.author,
              formatPublishedAt(source.publishedAt, locale),
            ]
              .filter((value): value is string => Boolean(value))
              .join(" · ");
            const cardClassName = cn(
              "focus-visible:ring-foreground/20 flex flex-col gap-1.5 py-3 outline-none focus-visible:ring-1",
            );
            const content = (
              <>
                {domain ? (
                  <div className="flex items-center gap-1.5">
                    <span className="bg-foreground/[0.06] text-foreground/45 flex size-4 shrink-0 items-center justify-center rounded text-[9px] font-medium">
                      {domain.charAt(0).toUpperCase()}
                    </span>
                    <span className={cn(mono, "text-foreground/40 truncate")}>
                      {domain}
                    </span>
                  </div>
                ) : null}
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="text-foreground/90 text-[14px] leading-snug font-medium">
                    {source.title}
                  </span>
                  {source.snippet ? (
                    <span className="source-raw-text text-foreground/80 whitespace-pre-wrap text-[14px] leading-relaxed">
                      {source.snippet}
                    </span>
                  ) : null}
                  {source.location ? <span className="text-foreground/60 text-xs leading-relaxed">{source.location}</span> : null}
                  {source.identity ? <p className="source-identity">{source.identity}</p> : null}
                  {meta ? (
                    <span className="text-foreground/40 truncate text-xs">
                      {meta}
                    </span>
                  ) : null}
                </div>
              </>
            );

            if (href) {
              return (
                <a
                  key={`${source.url ?? source.domain ?? source.title}-${index}`}
                  data-slot="source-card"
                  data-source-id={source.sourceId}
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={cardClassName}
                >
                  {content}
                  <span className="sr-only">(opens in a new tab)</span>
                </a>
              );
            }

            return (
              <div
                key={`${source.url ?? source.domain ?? source.title}-${index}`}
                data-slot="source-card"
                data-source-id={source.sourceId}
                className={cardClassName}
              >
                {content}
              </div>
            );
          })}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
