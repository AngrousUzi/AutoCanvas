<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { useRoute, useRouter } from "vue-router";
import { useApiQuery as query } from "../queries";
import { useWorkspace } from "../useWorkspace";
import { api, fileUrl } from "../api";
import { date } from "../format";
import {
  groupArtifacts,
  slideFiles,
  transcriptFiles,
  lectureTitle,
  timecode,
  fileLabel,
} from "../workspace";
import type { ArtifactLesson, OutputFile } from "../workspace";
import DocumentReader from "../components/DocumentReader.vue";
const route = useRoute(),
  router = useRouter();
const { courses, lectures, courseName } = useWorkspace();
const files = query("files", "/api/files", 15000),
  assignments = query("assignments", "/api/assignments");
const allFiles = computed<OutputFile[]>(() => files.data.value || []);
const validSection = (value: unknown) =>
  ["slides", "transcript", "data", "assignments"].includes(String(value))
    ? String(value)
    : "slides";
const search = ref(""),
  section = ref(validSection(route.query.section)),
  selectedKey = ref(""),
  documentPath = ref(""),
  viewer = ref<number | null>(null),
  dense = ref(false);
const selectedCourse = computed(() => String(route.query.course || ""));
const includeSamples = ref(route.query.sample === "1");
const allGroups = computed(() => groupArtifacts(allFiles.value));
const groups = computed(() =>
  allGroups.value.filter((g) => includeSamples.value || !g.sample),
);
const courseIds = computed(() =>
  [
    ...new Set([
      ...courses.value.map((c) => c.id),
      ...groups.value.map((g) => g.courseId),
    ]),
  ].filter((id) =>
    courseName(id).toLowerCase().includes(search.value.toLowerCase()),
  ),
);
const courseGroups = computed(() =>
  groups.value.filter((g) => g.courseId === selectedCourse.value),
);
const selected = computed(() =>
  courseGroups.value.find((g) => g.key === selectedKey.value),
);
const courseAssignments = computed<any[]>(() =>
  (assignments.data.value || []).filter(
    (a: any) => String(a.course_id) === selectedCourse.value,
  ),
);
function lesson(group: ArtifactLesson) {
  return (
    lectures.value.find(
      (l) =>
        l.course_id === group.courseId &&
        l.id === group.lectureId &&
        l.kind === "vod",
    ) ||
    lectures.value.find(
      (l) => l.course_id === group.courseId && l.id === group.lectureId,
    )
  );
}
watch(
  [courseGroups, () => route.query.lecture, () => route.query.sample],
  () => {
    if (
      selected.value &&
      (!route.query.lecture ||
        selected.value.lectureId === String(route.query.lecture))
    )
      return;
    selectedKey.value =
      (
        courseGroups.value.find(
          (g) =>
            g.lectureId === String(route.query.lecture) &&
            (route.query.sample !== "1" || g.sample),
        ) || courseGroups.value[0]
      )?.key || "";
  },
  { immediate: true },
);
watch(
  () => route.query.section,
  (value) => {
    if (value) section.value = validSection(value);
  },
);
watch(
  () => route.query.sample,
  (value) => {
    includeSamples.value = value === "1";
  },
);
function chooseCourse(id: string) {
  search.value = "";
  selectedKey.value = "";
  documentPath.value = "";
  section.value = "slides";
  router.push({ path: "/files", query: id ? { course: id } : {} });
}
function chooseLesson(group: ArtifactLesson) {
  selectedKey.value = group.key;
  documentPath.value = "";
  viewer.value = null;
  router.replace({
    path: "/files",
    query: {
      course: selectedCourse.value,
      lecture: group.lectureId,
      sample: group.sample ? "1" : undefined,
    },
  });
}
const slides = computed(() =>
  selected.value ? slideFiles(selected.value) : [],
);
const transcriptOptions = computed(() => {
  if (!selected.value) return [];
  const rows = transcriptFiles(selected.value);
  // JSONL includes the newest committed live fragments; completed replay uses JSON.
  const folders = [
    ...new Set(rows.map((f) => f.path.slice(0, f.path.lastIndexOf("/")))),
  ];
  return folders
    .map(
      (folder) =>
        rows.find(
          (f) =>
            f.path ===
              folder +
                (folder.endsWith("/live")
                  ? "/segments.jsonl"
                  : "/transcript.json") && f.size > 2,
        ) ||
        rows.find((f) => f.path === folder + "/segments.jsonl" && f.size > 0) ||
        rows.find((f) => f.path === folder + "/transcript.txt") ||
        rows.find((f) => f.path.startsWith(folder + "/")),
    )
    .filter((f): f is OutputFile => !!f)
    .sort(
      (a, b) =>
        Number(a.path.includes("/live/")) - Number(b.path.includes("/live/")),
    );
});
const dataOptions = computed(
  () =>
    selected.value?.files.filter(
      (f) =>
        /\.(json|jsonl|txt)$/i.test(f.path) &&
        !/\/(transcript\.(json|txt)|segments.jsonl)$/.test(f.path),
    ) || [],
);
const options = computed(() =>
  section.value === "transcript" ? transcriptOptions.value : dataOptions.value,
);
const document = computed(
  () =>
    options.value.find((f) => f.path === documentPath.value) ||
    options.value[0],
);
watch([() => selected.value?.key, section], () => {
  documentPath.value = "";
  viewer.value = null;
});
const manifest = computed(() =>
  selected.value?.files.find(
    (f) => f.name === "slides.json" && f.path.includes("/result/"),
  ),
);
const slideIndex = useQuery({
  queryKey: computed(() => [
    "slide-index",
    manifest.value?.path,
    manifest.value?.modified,
  ]),
  queryFn: () => api(fileUrl(manifest.value!.path)),
  enabled: computed(
    () => !!manifest.value && manifest.value.size < 2 * 1024 * 1024,
  ),
});
function slideTime(file: OutputFile) {
  const row = Array.isArray(slideIndex.data.value)
    ? slideIndex.data.value.find((s: any) => s.image === file.name)
    : undefined;
  const match = file.name.match(/_(\d+)s\./);
  return timecode(row?.time_seconds ?? (match ? Number(match[1]) : 0));
}
const viewed = computed(() =>
  viewer.value === null ? undefined : slides.value[viewer.value],
);
function navigateSlide(delta: number) {
  if (viewer.value !== null)
    viewer.value = Math.max(
      0,
      Math.min(slides.value.length - 1, viewer.value + delta),
    );
}
function keydown(event: KeyboardEvent) {
  if (viewer.value === null) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    navigateSlide(-1);
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    navigateSlide(1);
  }
}
onMounted(() => window.addEventListener("keydown", keydown));
onUnmounted(() => window.removeEventListener("keydown", keydown));
function assignmentFiles(id: string) {
  return allFiles.value.filter(
    (f) =>
      f.path.startsWith(`assignments/${selectedCourse.value}/${id}/`) &&
      !["assignment.json", "description.txt"].includes(f.name),
  );
}
const selectedAssignment = ref<any>(null);
const assignmentDocument = computed(() =>
  selectedAssignment.value
    ? allFiles.value.find(
        (f) =>
          f.path ===
          `assignments/${selectedCourse.value}/${selectedAssignment.value.id}/description.txt`,
      )
    : undefined,
);
</script>
<template>
  <div v-if="files.isPending.value" class="panel empty-state">
    正在整理课程产物…
  </div>
  <div v-else-if="files.isError.value" class="panel empty-state">
    <h3>暂时无法打开资料库</h3>
    <el-button @click="files.refetch()">重新加载</el-button>
  </div>
  <template v-else-if="!selectedCourse">
    <div class="toolbar">
      <el-input
        v-model="search"
        placeholder="找到你想回顾的课程…"
        aria-label="搜索产物课程"
        clearable
      /><el-button @click="files.refetch()">刷新产物</el-button>
    </div>
    <div class="library-intro">
      <p>
        {{ groups.filter((g) => !g.sample).length }} 个课次 ·
        {{ groups.reduce((n, g) => n + slideFiles(g).length, 0) }} 张课堂画面
      </p>
    </div>
    <div class="library-grid">
      <button
        v-for="id in courseIds"
        :key="id"
        class="library-card"
        @click="chooseCourse(id)"
      >
        <div class="library-card-body">
          <h3>{{ courseName(id) }}</h3>
          <p>
            {{ groups.filter((g) => g.courseId === id).length }} 个课次
            <span
              >·
              {{
                (assignments.data.value || []).filter(
                  (a: any) => String(a.course_id) === id,
                ).length
              }}
              份作业</span
            >
          </p>
          <div class="collection-features">
            <span>
              {{
                groups
                  .filter((g) => g.courseId === id)
                  .reduce((n, g) => n + slideFiles(g).length, 0)
              }}
              张画面</span
            ><span>
              {{
                groups.filter(
                  (g) =>
                    g.courseId === id &&
                    transcriptFiles(g).some((f) => f.size > 2),
                ).length
              }}
              份转写</span
            >
          </div>
        </div>
      </button>
    </div>
    <div v-if="!courseIds.length" class="panel empty-state">
      <h3>{{ search ? "没有找到这门课程" : "资料库还没有内容" }}</h3>
      <p>课程处理完成后，转写和课堂画面会自动归档到这里。</p>
      <RouterLink to="/courses">查看待执行课程 →</RouterLink>
    </div>
  </template>
  <template v-else>
    <div class="workspace-toolbar">
      <div class="inline-actions">
        <el-button @click="chooseCourse('')">← 全部课程</el-button>
        <h2 class="inline-title">{{ courseName(selectedCourse) }}</h2>
      </div>
      <el-button @click="files.refetch()">刷新产物</el-button>
    </div>
    <div class="library-workspace">
      <div class="lesson-rail">
        <div class="rail-heading">
          <b>课次目录</b><small>{{ courseGroups.length }} 节</small>
        </div>
        <label class="sample-toggle"
          ><el-checkbox v-model="includeSamples"
            >包含片段试跑</el-checkbox
          ></label
        >
        <div v-if="!courseGroups.length" class="rail-empty">
          暂无课堂产物，可查看作业或等待处理完成。
        </div>
        <button
          v-for="group in courseGroups"
          :key="group.key"
          class="lesson-selector"
          :class="{
            selected: selected?.key === group.key && section !== 'assignments',
          }"
          @click="
            chooseLesson(group);
            if (section === 'assignments') section = 'slides';
          "
        >
          <small>{{ date(lesson(group)?.begin || group.modified) }}</small
          ><b
            >{{ lectureTitle(lesson(group))
            }}<em v-if="group.sample">试跑</em></b
          ><span
            >{{ slideFiles(group).length }} 张画面 ·
            {{
              transcriptFiles(group).some((f) => f.size > 2)
                ? "有转写"
                : "暂无转写"
            }}</span
          ></button
        ><button
          class="assignment-selector"
          :class="{ selected: section === 'assignments' }"
          @click="section = 'assignments'"
        >
          <span>作业与附件</span><small>{{ courseAssignments.length }}</small>
        </button>
      </div>
      <section class="library-content panel">
        <div class="artifact-heading">
          <h2>
            {{
              section === "assignments"
                ? "作业与附件"
                : selected
                  ? lectureTitle(lesson(selected))
                  : "课堂产物"
            }}
          </h2>
          <p v-if="selected && section !== 'assignments'">
            {{ date(lesson(selected)?.begin || selected.modified) }}
            <span v-if="selected.sample">· 片段试跑</span>
          </p>
        </div>
        <div
          v-if="section !== 'assignments'"
          class="artifact-tabs"
          role="tablist"
          aria-label="产物类型"
        >
          <button
            v-for="[key, label, count] in [
              ['slides', 'Slides 图片墙', slides.length],
              ['transcript', '课堂转写', transcriptOptions.length],
              ['data', '课堂数据', dataOptions.length],
            ]"
            :key="key"
            role="tab"
            :aria-selected="section === key"
            :class="{ selected: section === key }"
            @click="section = String(key)"
          >
            {{ label }} <small>{{ count }}</small>
          </button>
        </div>
        <template v-if="section === 'slides'"
          ><div v-if="slides.length" class="gallery-toolbar">
            <span>{{ slides.length }} 张画面 · 点击放大，方向键切换</span
            ><button class="text-button" @click="dense = !dense">
              {{ dense ? "舒展视图" : "紧凑视图" }}
            </button>
          </div>
          <div class="slide-wall" :class="{ dense }">
            <button
              v-for="(file, i) in slides"
              :key="file.path"
              class="slide-card"
              :aria-label="'打开第 ' + (i + 1) + ' 张画面，' + slideTime(file)"
              @click="viewer = i"
            >
              <div class="slide-thumbnail">
                <img
                  :src="fileUrl(file.path)"
                  :alt="'第 ' + (i + 1) + ' 张课堂画面'"
                  loading="lazy"
                /><span>↗</span>
              </div>
              <div class="slide-caption">
                <b>{{ String(i + 1).padStart(2, "0") }}</b
                ><time>{{ slideTime(file) }}</time>
              </div>
            </button>
          </div>
          <div v-if="!slides.length" class="empty-state">
            <div class="empty-symbol">▧</div>
            <h3>这节课还没有 Slides</h3>
            <p>画面抽取完成后，会在这里自动整理成图片墙。</p>
            <RouterLink
              :to="{ path: '/courses', query: { course: selectedCourse } }"
              >查看课程处理 →</RouterLink
            >
          </div></template
        >
        <template v-else-if="section === 'transcript' || section === 'data'"
          ><div v-if="options.length > 1" class="document-select">
            <el-select
              :model-value="document?.path"
              @update:model-value="(v: unknown) => (documentPath = String(v))"
              aria-label="选择阅读内容"
              ><el-option
                v-for="file in options"
                :key="file.path"
                :value="file.path"
                :label="
                  section === 'transcript'
                    ? file.path.includes('/live/')
                      ? '直播转写'
                      : '回放转写'
                    : fileLabel(file)
                "
            /></el-select>
          </div>
          <DocumentReader v-if="document" :file="document" />
          <div v-else class="empty-state">
            <div class="empty-symbol">≋</div>
            <h3>
              {{
                section === "transcript" ? "转写内容还未生成" : "暂无课堂数据"
              }}
            </h3>
            <p>处理完成后即可在这里阅读。</p>
          </div></template
        >
        <template v-else-if="section === 'assignments'"
          ><article
            v-for="assignment in courseAssignments"
            :key="assignment.id"
            class="assignment-card"
          >
            <span class="kind-chip">课程作业</span>
            <RouterLink
              :to="{
                path: '/homework',
                query: { id: 'canvas:' + selectedCourse + ':' + assignment.id },
              }"
              >在作业工作台打开 →</RouterLink
            >
            <h3>{{ assignment.name }}</h3>
            <p class="muted">
              截止时间 {{ date(assignment.due_at) }}
              <span v-if="assignment.points_possible != null"
                >· {{ assignment.points_possible }} 分</span
              >
            </p>
            <p class="assignment-excerpt">
              {{
                String(assignment.description || "暂无正文说明").slice(0, 200)
              }}
            </p>
            <el-button
              type="primary"
              plain
              @click="selectedAssignment = assignment"
              >阅读作业</el-button
            >
            <div
              v-if="assignmentFiles(String(assignment.id)).length"
              class="attachment-list"
            >
              <a
                v-for="file in assignmentFiles(String(assignment.id))"
                :key="file.path"
                :href="fileUrl(file.path, true)"
                ><span>↓ {{ file.name.replace(/^\d+-/, "") }}</span
                ><small>{{ (file.size / 1024).toFixed(0) }} KB</small></a
              >
            </div>
            <p
              v-if="
                assignment.attachments?.some((a: any) => a.status === 'failed')
              "
              class="failure-hint"
            >
              部分附件未下载成功，可在待执行课程中同步这门课程。
            </p>
          </article>
          <div v-if="!courseAssignments.length" class="empty-state">
            <h3>暂时没有作业</h3>
            <p>同步到的作业正文和附件会出现在这里。</p>
          </div></template
        >
      </section>
    </div>
  </template>
  <el-dialog
    :model-value="viewer !== null"
    :title="'课堂画面 · ' + ((viewer ?? 0) + 1) + ' / ' + slides.length"
    width="min(1200px,96vw)"
    class="slide-dialog"
    @close="viewer = null"
    ><template v-if="viewed"
      ><img
        class="large-slide"
        :src="fileUrl(viewed.path)"
        :alt="'课堂画面 ' + ((viewer ?? 0) + 1)"
      />
      <div class="slide-viewer-toolbar">
        <el-button :disabled="viewer === 0" @click="navigateSlide(-1)"
          >← 上一张</el-button
        ><span>{{ slideTime(viewed) }}</span
        ><a :href="fileUrl(viewed.path, true)">下载原图 ↗</a
        ><el-button
          :disabled="viewer === slides.length - 1"
          @click="navigateSlide(1)"
          >下一张 →</el-button
        >
      </div></template
    ></el-dialog
  >
  <el-dialog
    :model-value="!!selectedAssignment"
    :title="selectedAssignment?.name || '作业详情'"
    width="min(860px,94vw)"
    @close="selectedAssignment = null"
    ><DocumentReader v-if="assignmentDocument" :file="assignmentDocument" />
    <p v-else class="reading-text">
      {{ selectedAssignment?.description || "暂无正文" }}
    </p></el-dialog
  >
</template>
