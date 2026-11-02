// Pure timeline logic for the lesson review tab: slides on one side, transcript
// on the other, both mapped onto one time axis owned by the transcript.

export interface SlideRecord {
  slide_number?: number;
  time_seconds?: number;
  last_seen_seconds?: number;
  image?: string;
  matched_sample_frames?: number[];
}
export interface SlideImage {
  name: string;
  path: string;
}
export interface Occurrence {
  /** Index into the slide image list (same order as the slide wall). */
  slide: number;
  start: number;
  end: number;
  /** Index of the first occurrence of the same slide when this one is a return. */
  returnOf: number | null;
}
export interface Segment {
  start: number;
  end: number;
  text: string;
}
export interface TranscriptRow {
  kind: "segment" | "gap";
  /** Original start/end as written by the source (gap rows: the silent span). */
  start: number;
  end: number;
  text: string;
  /** Effective span used for scroll mapping; t0 never decreases between rows. */
  t0: number;
  t1: number;
}
export interface RowBox {
  top: number;
  height: number;
}

export const DEFAULT_INTERVAL = 5;
export const GAP_SECONDS = 20;

const finite = (value: unknown): number | null => {
  const n = typeof value === "string" && value.trim() ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
};
export const fileSeconds = (name: string) => {
  const match = name.match(/_(\d+)s\./);
  return match ? Number(match[1]) : null;
};

/** Sample interval in seconds: time_seconds = (frame - 1) * interval. */
export function sampleInterval(records: SlideRecord[]): number {
  const candidates = records
    .map((r) => {
      const frames = Array.isArray(r.matched_sample_frames)
        ? r.matched_sample_frames
        : [];
      const first = finite(frames[0]),
        time = finite(r.time_seconds);
      return first !== null && first > 1 && time !== null && time > 0
        ? time / (first - 1)
        : null;
    })
    .filter((v): v is number => v !== null)
    .sort((a, b) => a - b);
  if (!candidates.length) return DEFAULT_INTERVAL;
  return (
    Math.round(candidates[Math.floor(candidates.length / 2)] * 1000) / 1000
  );
}

/**
 * Rebuild the on-screen timeline. Consecutive sample frames showing the same
 * slide become one occurrence; frames that match no slide extend the previous
 * occurrence. Records without frame lists fall back to time_seconds, then to the
 * seconds in the image file name. Without a usable slides.json the file names
 * alone give the order.
 */
export function buildOccurrences(
  manifest: unknown,
  images: SlideImage[],
): Occurrence[] {
  const byName = new Map(images.map((image, i) => [image.name, i]));
  const records = (Array.isArray(manifest) ? manifest : []).filter(
    (r): r is SlideRecord =>
      !!r && typeof r === "object" && byName.has(String(r.image)),
  );
  const interval = sampleInterval(records);
  const points: { t: number; slide: number; order: number; last: number }[] =
    [];
  const seenFrames = new Set<number>();
  if (records.length) {
    records.forEach((record, r) => {
      const slide = byName.get(String(record.image))!;
      const frames = (
        Array.isArray(record.matched_sample_frames)
          ? record.matched_sample_frames
          : []
      )
        .map(finite)
        .filter((k): k is number => k !== null && k >= 1);
      if (frames.length) {
        for (const k of frames) {
          if (seenFrames.has(k)) continue;
          seenFrames.add(k);
          const t = (k - 1) * interval;
          points.push({ t, slide, order: k, last: t });
        }
      } else {
        const t =
          finite(record.time_seconds) ?? fileSeconds(images[slide].name) ?? 0;
        const last = Math.max(t, finite(record.last_seen_seconds) ?? t);
        points.push({ t, slide, order: r, last });
      }
    });
  } else {
    images.forEach((image, i) =>
      points.push({
        t: fileSeconds(image.name) ?? 0,
        slide: i,
        order: i,
        last: fileSeconds(image.name) ?? 0,
      }),
    );
  }
  points.sort((a, b) => a.t - b.t || a.order - b.order);
  const runs: Occurrence[] = [];
  const first = new Map<number, number>();
  let lastSeen = 0;
  for (const point of points) {
    const previous = runs[runs.length - 1];
    if (previous && previous.slide === point.slide) {
      lastSeen = Math.max(lastSeen, point.last);
      continue;
    }
    if (previous) previous.end = point.t;
    runs.push({
      slide: point.slide,
      start: point.t,
      end: point.t,
      returnOf: first.has(point.slide) ? first.get(point.slide)! : null,
    });
    if (!first.has(point.slide)) first.set(point.slide, runs.length - 1);
    lastSeen = point.last;
  }
  const tail = runs[runs.length - 1];
  if (tail) tail.end = Math.max(tail.start, lastSeen + interval);
  return runs;
}

/** Valid, time-sorted segments. Rows without text are dropped; end >= start. */
export function normalizeSegments(rows: unknown): Segment[] {
  if (!Array.isArray(rows)) return [];
  return rows
    .flatMap((row) => {
      if (!row || typeof row !== "object") return [];
      const r = row as Record<string, unknown>;
      const start = finite(r.start);
      const text = typeof r.text === "string" ? r.text.trim() : "";
      if (start === null || start < 0 || !text) return [];
      const end = finite(r.end);
      return [{ start, end: Math.max(start, end ?? start), text }];
    })
    .sort((a, b) => a.start - b.start);
}

/**
 * Transcript rows in reading order with gap rows where nothing was transcribed
 * for at least `gap` seconds (also before the first segment and up to `until`).
 * Each row gets an effective span [t0, t1] clipped so that time never runs
 * backwards across rows, even when segments overlap.
 */
export function buildRows(
  segments: Segment[],
  options: { gap?: number; until?: number } = {},
): TranscriptRow[] {
  const gap = options.gap ?? GAP_SECONDS;
  const rows: TranscriptRow[] = [];
  const pushGap = (start: number, end: number) =>
    rows.push({ kind: "gap", start, end, text: "", t0: start, t1: end });
  let reached = 0;
  for (const segment of segments) {
    if (segment.start - reached >= gap) pushGap(reached, segment.start);
    rows.push({
      kind: "segment",
      ...segment,
      t0: segment.start,
      t1: segment.end,
    });
    reached = Math.max(reached, segment.end);
  }
  if (
    segments.length &&
    options.until !== undefined &&
    options.until - reached >= gap
  )
    pushGap(reached, options.until);
  for (let i = 0; i < rows.length; i++) {
    const next = rows[i + 1];
    if (i > 0) rows[i].t0 = Math.max(rows[i].t0, rows[i - 1].t1);
    rows[i].t1 = Math.max(
      rows[i].t0,
      next ? Math.min(rows[i].t1, next.start) : rows[i].t1,
    );
  }
  return rows;
}

/** Last index whose key is <= value, or -1. Keys must be non-decreasing. */
export function lastAtOrBefore(
  length: number,
  key: (i: number) => number,
  value: number,
) {
  let lo = 0,
    hi = length - 1,
    found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (key(mid) <= value) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/** Occurrence on screen at time t; clamps to the first and last occurrence. */
export function occurrenceAt(occurrences: Occurrence[], t: number) {
  if (!occurrences.length) return -1;
  return Math.max(
    0,
    lastAtOrBefore(occurrences.length, (i) => occurrences[i].start, t),
  );
}

/** Transcript time at content offset y (pixels from the top of the row list). */
export function timeAtOffset(
  rows: TranscriptRow[],
  boxes: RowBox[],
  y: number,
) {
  const count = Math.min(rows.length, boxes.length);
  if (!count) return 0;
  const i = lastAtOrBefore(count, (k) => boxes[k].top, y);
  if (i < 0) return rows[0].t0;
  const box = boxes[i],
    row = rows[i];
  if (y < box.top + box.height) {
    const fraction = box.height > 0 ? (y - box.top) / box.height : 0;
    return row.t0 + Math.min(1, Math.max(0, fraction)) * (row.t1 - row.t0);
  }
  // Between rows: the next row's start; after the last row: its end.
  return i + 1 < count ? rows[i + 1].t0 : row.t1;
}

/**
 * Row and fraction for time t: the row whose span contains t at the matching
 * offset, otherwise the nearest following row (or the end of the last row).
 */
export function rowAtTime(rows: TranscriptRow[], t: number) {
  if (!rows.length) return { row: -1, fraction: 0 };
  const i = lastAtOrBefore(rows.length, (k) => rows[k].t0, t);
  if (i < 0) return { row: 0, fraction: 0 };
  const row = rows[i];
  if (t < row.t1) return { row: i, fraction: (t - row.t0) / (row.t1 - row.t0) };
  if (t === row.t0) return { row: i, fraction: 0 };
  return i + 1 < rows.length
    ? { row: i + 1, fraction: 0 }
    : { row: i, fraction: 1 };
}

export function offsetAtTime(
  rows: TranscriptRow[],
  boxes: RowBox[],
  t: number,
) {
  const { row, fraction } = rowAtTime(rows, t);
  const box = boxes[row];
  return box ? box.top + fraction * box.height : 0;
}

/**
 * Content offset of the reading line for a scroll position. The line sits a
 * third of the way down the pane, but starts at the very top when scrolled to
 * the start and reaches the very bottom when scrolled to the end, so the first
 * and last rows can be read too. Continuous and strictly increasing.
 */
export function readingLine(
  scrollTop: number,
  viewport: number,
  maxScroll: number,
) {
  const s = Math.min(Math.max(0, scrollTop), Math.max(0, maxScroll));
  if (maxScroll <= 0) return 0;
  if (maxScroll < viewport) return s + (viewport * s) / maxScroll;
  const a = viewport / 3;
  return s + a * Math.min(1, s / a) + Math.max(0, s - (maxScroll - 2 * a));
}

/** Inverse of readingLine, clamped to the scroll range. */
export function scrollForLine(
  line: number,
  viewport: number,
  maxScroll: number,
) {
  if (maxScroll <= 0) return 0;
  let lo = 0,
    hi = maxScroll;
  if (line <= readingLine(lo, viewport, maxScroll)) return 0;
  if (line >= readingLine(hi, viewport, maxScroll)) return maxScroll;
  for (let n = 0; n < 40 && hi - lo > 0.25; n++) {
    const mid = (lo + hi) / 2;
    if (readingLine(mid, viewport, maxScroll) < line) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}

export const gapHeight = (seconds: number) =>
  Math.round(Math.min(160, Math.max(36, seconds * 0.4)));

export interface TranscriptSource {
  key: "replay" | "platform" | "live";
  label: string;
  path: string;
  /** In-progress files (segments.jsonl of a replay job) may still grow. */
  partial: boolean;
}

/** Transcript sources of one lesson folder, in selector order. */
export function transcriptSources(
  lessonKey: string,
  files: { path: string; size: number }[],
): TranscriptSource[] {
  const find = (name: string, min: number) =>
    files.find((f) => f.path === `${lessonKey}/${name}` && f.size > min);
  const finished = find("vod_asr/transcript.json", 2),
    progress = find("vod_asr/segments.jsonl", 0),
    platform = find("platform/transcript.json", 2),
    live = find("live/segments.jsonl", 0) || find("live/transcript.json", 2);
  const rows: TranscriptSource[] = [];
  const replay = finished || progress;
  if (replay)
    rows.push({
      key: "replay",
      label: finished ? "回放转写" : "回放转写（生成中）",
      path: replay.path,
      partial: !finished,
    });
  if (platform)
    rows.push({
      key: "platform",
      label: "平台原始转写",
      path: platform.path,
      partial: false,
    });
  if (live)
    rows.push({
      key: "live",
      label: "直播转写",
      path: live.path,
      partial: live.path.endsWith(".jsonl"),
    });
  return rows;
}

/** Finished replay, then platform, then in-progress replay, then live. */
export function defaultSource(sources: TranscriptSource[]) {
  const rank = (s: TranscriptSource) =>
    s.key === "replay" ? (s.partial ? 2 : 0) : s.key === "platform" ? 1 : 3;
  return [...sources].sort((a, b) => rank(a) - rank(b))[0];
}
