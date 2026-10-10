<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import { useWorkspace } from "../useWorkspace";
import { queueLessons, lectureTitle } from "../workspace";
import { date, kinds, statuses, tone } from "../format";
import LectureStreams from "../components/LectureStreams.vue";
import StreamPlayer from "../components/StreamPlayer.vue";
import LiveTranscript from "../components/LiveTranscript.vue";
import LessonAttendance from "../components/LessonAttendance.vue";
import ExecutionDetail from "../components/ExecutionDetail.vue";
const { lectures, runs, courseName, lectureQuery, runQuery } = useWorkspace();
const route = useRoute();
const search = ref("");
const detailId = ref<string | null>(null);
const active = computed(() =>
  queueLessons(lectures.value, runs.value).filter(
    (item) => item.state === "running",
  ),
);
const filtered = computed(() =>
  active.value.filter(
    (item) =>
      (!route.query.course || item.lecture.course_id === route.query.course) &&
      `${courseName(item.lecture.course_id)} ${lectureTitle(item.lecture)} ${item.lecture.course_id} ${item.lecture.id}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
  ),
);
</script>
<template>
  <div class="queue-summary">
    <div>
      <span class="live-indicator" :class="{ muted: !active.length }"></span
      ><b>{{ active.length }} 节课程执行中</b>
      <p>视频默认静音，可通过各镜头播放控件开启声音。</p>
    </div>
    <RouterLink to="/courses">待执行课程 →</RouterLink>
  </div>
  <div class="toolbar">
    <el-input
      v-model="search"
      clearable
      placeholder="搜索课程名称、课程编号或课次编号"
      aria-label="搜索执行中课程"
    /><RouterLink v-if="route.query.course" to="/running"
      >显示全部课程</RouterLink
    >
  </div>
  <div
    v-if="lectureQuery.isError.value || runQuery.isError.value"
    class="panel empty-state"
  >
    <h3>课堂信息加载失败</h3>
    <el-button
      @click="
        lectureQuery.refetch();
        runQuery.refetch();
      "
      >重试</el-button
    >
  </div>
  <div
    v-else-if="lectureQuery.isPending.value || runQuery.isPending.value"
    class="panel empty-state"
  >
    正在加载课堂…
  </div>
  <div v-else-if="!filtered.length" class="panel empty-state">
    <h3>
      {{
        search || route.query.course
          ? "没有符合条件的执行中课程"
          : "当前没有执行中课程"
      }}
    </h3>
    <p>任务开始后，课堂画面与转写会自动出现在这里。</p>
  </div>
  <article
    v-for="item in filtered"
    :key="item.key"
    class="lesson-card working running-classroom"
  >
    <div class="section-head">
      <div>
        <div class="lesson-meta">
          <span class="kind-chip">{{
            item.lecture.kind === "live" ? "◉ 直播课堂" : "▷ 回放处理"
          }}</span
          ><el-tag type="success">执行中</el-tag>
        </div>
        <h2>{{ courseName(item.lecture.course_id) }}</h2>
        <p
          v-if="
            lectureTitle(item.lecture) !== courseName(item.lecture.course_id)
          "
        >
          {{ lectureTitle(item.lecture) }}
        </p>
      </div>
      <RouterLink
        :to="{ path: '/courses', query: { course: item.lecture.course_id } }"
        >自动化设置 ↗</RouterLink
      >
    </div>
    <dl class="classroom-facts">
      <div>
        <dt>课程编号</dt>
        <dd>{{ item.lecture.course_id }}</dd>
      </div>
      <div>
        <dt>课次编号</dt>
        <dd>{{ item.lecture.id }}</dd>
      </div>
      <div>
        <dt>开始时间</dt>
        <dd>{{ date(item.lecture.begin) }}</dd>
      </div>
      <div>
        <dt>结束时间</dt>
        <dd>{{ date(item.lecture.end) }}</dd>
      </div>
    </dl>
    <div class="lesson-steps">
      <div v-for="step in item.steps" :key="step.kind" class="step">
        <span class="step-icon" :class="step.run?.status">{{
          step.run?.status === "running" ? "◉" : "○"
        }}</span
        ><span>{{ kinds[step.kind] }}</span
        ><el-tag :type="tone(step.run?.status || 'pending')">{{
          step.run ? statuses[step.run.status] : "待安排"
        }}</el-tag
        ><el-button v-if="step.run" link @click="detailId = step.run.id"
          >执行详情</el-button
        >
      </div>
    </div>
    <LessonAttendance
      v-if="item.lecture.kind === 'live'"
      :lecture="item.lecture"
    />
    <LectureStreams :lecture="item.lecture" eager
      ><template #default="{ links }"
        ><div class="classroom-monitor">
          <div class="camera-stack">
            <StreamPlayer
              v-for="index in 2"
              :key="index"
              :url="links[index - 1]?.url"
              :label="`镜头 ${index}${links[index - 1]?.view ? ' · 画面 ' + links[index - 1]?.view : ''}`"
            />
          </div>
          <div class="transcript-column">
            <LiveTranscript :lecture="item.lecture" />
          </div></div></template
    ></LectureStreams>
  </article>
  <ExecutionDetail :id="detailId" @close="detailId = null" />
</template>
<style scoped>
.running-classroom {
  padding: 0;
}
.classroom-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 16px 36px;
  padding: 16px 0;
  margin: 0 0 12px;
  border-bottom: 1px solid var(--ochrelune-border);
}
dt {
  font-size: 12px;
  color: var(--ochrelune-text-secondary);
  margin-bottom: 5px;
}
dd {
  margin: 0;
  font-size: 14px;
}
.classroom-monitor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
  margin-top: 20px;
}
.camera-stack {
  display: grid;
  gap: 14px;
  min-width: 0;
}
.transcript-column {
  position: relative;
  min-width: 0;
}
.transcript-column > * {
  position: absolute;
  inset: 0;
}
@media (max-width: 900px) {
  .classroom-monitor {
    grid-template-columns: 1fr;
  }
  .transcript-column {
    height: 480px;
  }
  .running-classroom {
    padding: 0;
  }
}
</style>
