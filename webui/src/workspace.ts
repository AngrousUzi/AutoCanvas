export interface Course {
  id: string;
  name: string;
  active?: boolean;
}
export interface Lecture {
  id: string;
  course_id: string;
  kind: string;
  name?: string;
  title?: string;
  begin?: string;
  end?: string;
}
export interface Run {
  id: string;
  kind: string;
  course_id: string;
  lecture_id: string;
  status: string;
  attempts: number;
  updated: number;
  created: number;
  due?: number;
  error?: string;
  artifact?: string;
  options?: Record<string, unknown>;
}
export interface OutputFile {
  path: string;
  name: string;
  size: number;
  modified: number;
}
export const activeStatuses = new Set(["pending", "running", "needs_login"]);
export const courseKinds = new Set(["live", "vod_asr", "vod_slides"]);
export const isActive = (run: Run) => activeStatuses.has(run.status);
export const lectureTitle = (lecture?: Lecture) =>
  lecture?.title || lecture?.name || "课堂记录";
export function timecode(seconds = 0) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}
export function findLecture(run: Run, lectures: Lecture[]) {
  return lectures.find(
    (l) =>
      l.course_id === run.course_id &&
      l.id === run.lecture_id &&
      l.kind === (run.kind === "live" ? "live" : "vod"),
  );
}
export interface QueueLesson {
  key: string;
  lecture: Lecture;
  steps: { kind: string; run?: Run }[];
  state: "running" | "needs_login" | "pending" | "upcoming" | "ready";
}
export function queueLessons(
  lectures: Lecture[],
  runs: Run[],
  now = Date.now(),
): QueueLesson[] {
  const items: QueueLesson[] = [];
  for (const lecture of lectures) {
    const kinds =
      lecture.kind === "live" ? ["live"] : ["vod_asr", "vod_slides"];
    const steps = kinds.map((kind) => ({
      kind,
      run: runs.find(
        (r) =>
          r.course_id === lecture.course_id &&
          r.lecture_id === lecture.id &&
          r.kind === kind,
      ),
    }));
    const active = steps.some((s) => s.run && isActive(s.run));
    const missing = steps.some((s) => !s.run);
    const ended =
      lecture.kind === "live" &&
      (!lecture.end || Date.parse(lecture.end) <= now);
    if (!active && (ended || !missing)) continue;
    const state = steps.some((s) => s.run?.status === "running")
      ? "running"
      : steps.some((s) => s.run?.status === "needs_login")
        ? "needs_login"
        : steps.some((s) => s.run?.status === "pending")
          ? "pending"
          : lecture.kind === "live" && Date.parse(lecture.begin || "") > now
            ? "upcoming"
            : "ready";
    items.push({
      key: `${lecture.course_id}:${lecture.kind}:${lecture.id}`,
      lecture,
      steps,
      state,
    });
  }
  const priority = {
    running: 0,
    needs_login: 1,
    pending: 2,
    ready: 3,
    upcoming: 4,
  };
  return items.sort(
    (a, b) =>
      priority[a.state] - priority[b.state] ||
      Date.parse(a.lecture.begin || "") - Date.parse(b.lecture.begin || ""),
  );
}
export interface ArtifactLesson {
  key: string;
  courseId: string;
  lectureId: string;
  sample: boolean;
  files: OutputFile[];
  modified: number;
}
export function groupArtifacts(files: OutputFile[]): ArtifactLesson[] {
  const groups = new Map<string, ArtifactLesson>();
  for (const file of files) {
    const parts = file.path.split("/");
    if (
      parts.some((p) => ["pending", "previous"].includes(p)) ||
      /\.(tmp|part)$/.test(file.path)
    )
      continue;
    const sample = parts[0] === "samples";
    if (parts[0] !== "outputs" && !sample) continue;
    const offset = sample ? 2 : 1;
    const courseId = parts[offset],
      lectureId = parts[offset + 1];
    if (!courseId || !lectureId || parts.length < offset + 4) continue;
    const key = `${sample ? parts.slice(0, offset).join("/") : "outputs"}/${courseId}/${lectureId}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, courseId, lectureId, sample, files: [], modified: 0 };
      groups.set(key, group);
    }
    group.files.push(file);
    group.modified = Math.max(group.modified, file.modified);
  }
  return [...groups.values()].sort((a, b) => b.modified - a.modified);
}
export const slideFiles = (group: ArtifactLesson) =>
  group.files
    .filter((f) =>
      /\/(?:result\/)?[^/]+_slide_.*\.(?:jpg|png|webp)$/i.test(f.path),
    )
    .sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
export const transcriptFiles = (group: ArtifactLesson) =>
  group.files.filter((f) =>
    /\/(transcript\.json|segments\.jsonl|transcript\.txt)$/.test(f.path),
  );
const transcriptFolder = (path: string) => path.slice(0, path.lastIndexOf("/"));
export const transcriptRank = (path: string) =>
  path.includes("/live/")
    ? 2
    : transcriptFolder(path).endsWith("/platform")
      ? 1
      : 0;
export const transcriptLabel = (path: string) =>
  path.includes("/live/")
    ? "直播转写"
    : transcriptFolder(path).endsWith("/platform")
      ? "平台原始转写"
      : "回放转写";
/** JSON Lines rows; lines that are not complete JSON yet are counted, not thrown. */
export function parseJsonLines(text: string) {
  let invalid = 0;
  const rows = text
    .split("\n")
    .filter(Boolean)
    .flatMap((line) => {
      try {
        return [JSON.parse(line)];
      } catch {
        invalid++;
        return [];
      }
    });
  return { rows, invalid };
}
export function fileLabel(file: OutputFile) {
  const names: Record<string, string> = {
    "transcript.json": "分段转写",
    "transcript.txt": "文字稿",
    "raw.json": "平台原始数据",
    "segments.jsonl": "实时转写",
    "slides.json": "画面时间索引",
    "qr.json": "二维码检测报告",
    "events.jsonl": "直播事件",
    "assignment.json": "作业详情",
    "description.txt": "作业正文",
    "contact_sheet.jpg": "画面总览",
  };
  return names[file.name] || file.name;
}
export function errorAdvice(error?: string) {
  if (!error) return "";
  if (error === "AuthenticationRequired")
    return "正在等待认证恢复。系统会自动尝试，也可前往登录页检查。";
  if (
    ["MediaError", "RemoteError", "Timeout", "ConnectionError"].includes(error)
  )
    return "视频源或网络暂不可用。确认课程资源可访问后，可以重新执行。";
  if (error === "TypeError")
    return "处理程序遇到数据格式问题。可查看服务日志定位，重复重试可能无法解决。";
  return "处理未能完成。查看详情与服务日志，排除问题后重新执行。";
}
