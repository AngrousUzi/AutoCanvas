<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { useAction, useApiQuery as query } from "../queries";
import { useWorkspace } from "../useWorkspace";
import { date, kinds, statuses, tone } from "../format";
import {
  queueLessons,
  lectureTitle,
  isActive,
  findLecture,
} from "../workspace";
import type { QueueLesson } from "../workspace";
import ExecutionDetail from "../components/ExecutionDetail.vue";
import LectureStreams from "../components/LectureStreams.vue";
const route = useRoute(),
  router = useRouter();
const { courses, lectures, runs, courseName, lectureQuery } = useWorkspace();
const { action, busy } = useAction();
const health = query("health", "/health", 10000),
  config = query("settings", "/api/settings"),
  sync = query("sync", "/api/sync");
const selectedCourse = computed(() => String(route.query.course || ""));
const search = ref(""),
  filter = ref("all"),
  detailId = ref<string | null>(null),
  manage = ref(false),
  ruleError = ref("");
const now = ref(Date.now());
const timer = setInterval(() => (now.value = Date.now()), 30000);
onUnmounted(() => clearInterval(timer));
const queue = computed(() =>
  queueLessons(lectures.value, runs.value, now.value),
);
const all = computed(() =>
  queue.value.filter((item) => item.state !== "running"),
);
const filtered = computed(() =>
  all.value.filter(
    (item) =>
      (!selectedCourse.value ||
        item.lecture.course_id === selectedCourse.value) &&
      (filter.value === "all" || item.state === filter.value) &&
      `${courseName(item.lecture.course_id)} ${lectureTitle(item.lecture)} ${date(item.lecture.begin)}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
  ),
);
const groups = computed(() =>
  [...new Set(filtered.value.map((i) => i.lecture.course_id))].map((id) => ({
    id,
    items: filtered.value.filter((i) => i.lecture.course_id === id),
  })),
);
const activeCount = computed(
  () => queue.value.filter((i) => i.state === "running").length,
);
const failedCount = computed(
  () => runs.value.filter((r) => r.status === "failed").length,
);
const paused = computed(
  () =>
    health.data.value?.automation?.paused ||
    !health.data.value?.scheduler_running,
);
const background = computed(() =>
  runs.value.filter(
    (r) =>
      isActive(r) &&
      (!["vod_asr", "vod_slides", "live"].includes(r.kind) ||
        !findLecture(r, lectures.value)),
  ),
);
const stateNames: Record<string, string> = {
  running: "执行中",
  needs_login: "等待认证",
  pending: "队列中",
  upcoming: "即将开始",
  ready: "待安排",
};
const rules = ref<Record<string, boolean>>({});
let ruleRequest = 0;
watch(
  selectedCourse,
  async (id) => {
    const request = ++ruleRequest;
    rules.value = {};
    ruleError.value = "";
    if (!id) return;
    try {
      const result = await api("/api/courses/" + id + "/rules");
      if (request === ruleRequest) rules.value = result;
    } catch {
      if (request === ruleRequest)
        ruleError.value = "课程规则读取失败，请重新选择课程。";
    }
  },
  { immediate: true },
);
async function setRule(key: string, value: unknown) {
  const result = await action(
    "/api/courses/" + selectedCourse.value + "/rules",
    { [key]: value },
    "PATCH",
  );
  if (result) rules.value = result;
}
async function schedule(item: QueueLesson) {
  for (const step of item.steps.filter((s) => !s.run)) {
    const result = await action("/api/process/" + step.kind, {
      course_id: item.lecture.course_id,
      lecture_id: item.lecture.id,
    });
    if (!result) break;
  }
}
function chooseCourse(value: unknown) {
  router.replace({
    query: { ...route.query, course: String(value || "") || undefined },
  });
}
function openRules(id: string) {
  chooseCourse(id);
  manage.value = true;
}
</script>
<template>
  <div class="queue-summary">
    <div>
      <span class="live-indicator" :class="{ muted: !activeCount }"></span
      ><b>{{
        activeCount ? `${activeCount} 节课程正在处理` : "当前没有正在处理的课程"
      }}</b>
      <p>
        {{
          paused
            ? "自动安排已暂停，手动发起的任务仍可执行。"
            : "新回放自动安排处理，直播在开课前进入监听。"
        }}
      </p>
    </div>
    <RouterLink v-if="activeCount" to="/running">查看执行中课堂 →</RouterLink>
    <el-button
      :loading="busy"
      @click="action('/api/automation', { paused: !paused })"
      >{{ paused ? "恢复自动安排" : "暂停自动安排" }}</el-button
    >
  </div>
  <div class="workspace-toolbar">
    <div class="segmented" aria-label="队列状态">
      <button
        v-for="[key, label] in [
          ['all', '全部待办'],
          ['pending', '队列中'],
          ['upcoming', '即将开始'],
          ['ready', '待安排'],
          ['needs_login', '等待认证'],
        ]"
        :key="key"
        :class="{ selected: filter === key }"
        :aria-pressed="filter === key"
        @click="filter = key"
      >
        {{ label
        }}<small>{{
          key === "all" ? all.length : all.filter((i) => i.state === key).length
        }}</small>
      </button>
    </div>
  </div>
  <div class="toolbar">
    <el-input
      v-model="search"
      placeholder="搜索课程或课次"
      aria-label="搜索待执行课程"
      clearable
    /><el-select
      :model-value="selectedCourse"
      @update:model-value="chooseCourse"
      placeholder="全部课程"
      aria-label="筛选课程"
      clearable
      style="width: 260px"
      ><el-option
        v-for="c in courses"
        :key="c.id"
        :value="c.id"
        :label="c.name" /></el-select
    ><el-button v-if="selectedCourse" @click="manage = !manage"
      >课程自动化设置</el-button
    >
  </div>
  <section v-if="manage && selectedCourse" class="panel">
    <div class="section-head">
      <h2>{{ courseName(selectedCourse) }} · 自动化设置</h2>
      <el-button link @click="manage = false">收起</el-button>
    </div>
    <el-alert
      v-if="ruleError"
      :title="ruleError"
      type="warning"
      :closable="false"
    />
    <p
      v-if="courses.find((c) => c.id === selectedCourse)?.active === false"
      class="muted"
    >
      此课程未参与自动化，请在全部设置中调整课程范围。
    </p>
    <div class="rule-panel">
      <label
        v-for="(label, key) in {
          asr: '自动转写',
          slides: '自动抽取 Slides',
          live: '自动监听直播',
        }"
        :key="key"
        >{{ label }}
        <el-switch
          :disabled="busy || !!ruleError"
          :model-value="rules[key] ?? config.data.value?.current['auto_' + key]"
          @change="(v: unknown) => setRule(key, v)" /></label
      ><el-button
        :loading="busy"
        @click="action('/api/sync', { course_id: selectedCourse })"
        >同步这门课程</el-button
      >
    </div>
    <el-alert
      v-if="
        sync.data.value?.some(
          (s: any) =>
            s.course_id === selectedCourse && s.status === 'video_unavailable',
        )
      "
      title="该课程暂未提供视频入口，作业仍可同步。"
      type="info"
      :closable="false"
    />
  </section>
  <div v-if="failedCount" class="notice-strip">
    <span>{{ failedCount }} 项历史执行未完成，可在执行记录中查看原因。</span
    ><RouterLink :to="{ path: '/executions', query: { status: 'failed' } }"
      >查看失败记录 →</RouterLink
    >
  </div>
  <div v-if="lectureQuery.isPending.value" class="panel empty-state">
    <h3>正在整理课程…</h3>
  </div>
  <div v-else-if="lectureQuery.isError.value" class="panel empty-state">
    <h3>课程暂时加载失败</h3>
    <el-button @click="lectureQuery.refetch()">重新加载</el-button>
  </div>
  <div v-else-if="!groups.length" class="panel empty-state">
    <div class="empty-symbol">✓</div>
    <h3>
      {{
        search || filter !== "all" || selectedCourse
          ? "没有符合条件的待办"
          : "课程处理已安排妥当"
      }}
    </h3>
    <p>已完成的内容在产物浏览中，历史状态保留在执行记录。</p>
    <div class="dialog-actions">
      <el-button
        v-if="search || filter !== 'all' || selectedCourse"
        @click="
          search = '';
          filter = 'all';
          chooseCourse('');
        "
        >清除筛选</el-button
      ><el-button type="primary" @click="router.push('/files')"
        >浏览课程产物</el-button
      >
    </div>
  </div>
  <section v-for="group in groups" :key="group.id" class="queue-course">
    <div class="section-head">
      <div class="course-section-title">
        <span class="course-avatar">{{
          courseName(group.id).slice(0, 1)
        }}</span>
        <div>
          <h2>{{ courseName(group.id) }}</h2>
          <p>{{ group.items.length }} 节待办</p>
        </div>
      </div>
      <el-button link @click="openRules(group.id)">自动化设置 ↗</el-button>
    </div>
    <article
      v-for="item in group.items"
      :key="item.key"
      class="lesson-card"
      :class="{ working: item.state === 'running' }"
    >
      <div class="lesson-meta">
        <span class="kind-chip">{{
          item.lecture.kind === "live" ? "直播" : "回放"
        }}</span
        ><span>{{ date(item.lecture.begin) }}</span
        ><el-tag
          :type="
            item.state === 'needs_login'
              ? 'danger'
              : item.state === 'running'
                ? 'success'
                : 'info'
          "
          >{{ stateNames[item.state] }}</el-tag
        >
      </div>
      <h3>{{ lectureTitle(item.lecture) }}</h3>
      <div class="lesson-steps">
        <div v-for="step in item.steps" :key="step.kind" class="step">
          <span class="step-icon" :class="step.run?.status">{{
            step.run?.status === "succeeded"
              ? "✓"
              : step.run?.status === "running"
                ? "◉"
                : "○"
          }}</span
          ><span>{{ kinds[step.kind] }}</span
          ><b>{{ step.run ? statuses[step.run.status] : "待安排" }}</b
          ><el-button v-if="step.run" link @click="detailId = step.run.id"
            >详情</el-button
          >
        </div>
      </div>
      <div
        v-if="item.state === 'running'"
        class="activity-track"
        aria-label="处理中"
      >
        <span></span>
      </div>
      <LectureStreams :lecture="item.lecture" />
      <div class="lesson-footer">
        <small>{{
          item.state === "upcoming"
            ? "按课程规则在开课前自动监听"
            : item.state === "needs_login"
              ? "认证恢复后自动继续"
              : paused
                ? "自动安排已暂停，可手动执行"
                : "状态实时更新"
        }}</small>
        <div class="inline-actions">
          <RouterLink v-if="item.state === 'needs_login'" to="/auth"
            >检查登录 →</RouterLink
          ><el-button
            v-if="item.steps.some((s) => !s.run) && item.state !== 'upcoming'"
            :loading="busy"
            type="primary"
            plain
            @click="schedule(item)"
            >安排处理</el-button
          ><el-button
            v-if="item.steps.some((s) => s.run?.artifact)"
            @click="
              router.push({
                path: '/files',
                query: { course: group.id, lecture: item.lecture.id },
              })
            "
            >查看已有产物</el-button
          >
        </div>
      </div>
    </article>
  </section>
  <section v-if="background.length" class="panel">
    <h2>后台同步与其他任务</h2>
    <button
      v-for="run in background"
      :key="run.id"
      class="plain-run"
      @click="detailId = run.id"
    >
      <span>{{ kinds[run.kind] }} · {{ courseName(run.course_id) }}</span
      ><el-tag :type="tone(run.status)">{{ statuses[run.status] }}</el-tag>
    </button>
  </section>
  <ExecutionDetail :id="detailId" @close="detailId = null" />
</template>
