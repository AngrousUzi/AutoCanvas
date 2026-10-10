<script setup lang="ts">
import { computed, ref } from "vue";
import MaterialIcon from "../components/MaterialIcon.vue";

import { useApiQuery as query, useAction } from "../queries";
import { date, statuses, kinds, tone } from "../format";
import ExecutionDetail from "../components/ExecutionDetail.vue";
const { action, busy } = useAction();
const courses = query("courses", "/api/courses");
const courseRows = computed<any[]>(() => courses.data.value || []);
function courseName(id: string) {
  return courseRows.value.find((r) => r.id === id)?.name || id;
}
const executions = query("executions", "/api/executions");
const runs = computed<any[]>(() => executions.data.value || []);
const health = query("health", "/health", 10000),
  lectures = query("lectures", "/api/lectures");
const executionDetail = ref<any>(null);
</script>
<template>
  <div class="stats">
    <div>
      <span>已同步课程</span
      ><strong>{{ courseRows.length }}<small>门课程</small></strong>
    </div>
    <div>
      <span>视频记录</span
      ><strong
        >{{ (lectures.data.value || []).length }}<small>条记录</small></strong
      >
    </div>
    <div>
      <span>正在处理</span
      ><strong
        >{{ runs.filter((r) => r.status === "running").length
        }}<small>项执行</small></strong
      >
    </div>
    <div>
      <span>已完成</span
      ><strong
        >{{ runs.filter((r) => r.status === "succeeded").length
        }}<small>项执行</small></strong
      >
    </div>
  </div>
  <section class="automation-status" aria-label="自动化状态">
    <div class="automation-state">
      <span id="automation-label">{{
        health.isError.value
          ? "自动化状态不可用"
          : !health.data.value
            ? "正在读取自动化状态…"
            : !health.data.value.scheduler_running
              ? "调度未启动"
              : health.data.value.automation?.paused
                ? "自动化已暂停"
                : "自动化已启用"
      }}</span>
      <el-switch
        aria-label="启用自动化"
        :model-value="
          Boolean(
            health.data.value?.scheduler_running &&
            !health.data.value?.automation?.paused,
          )
        "
        :loading="busy"
        :disabled="
          busy || health.isError.value || !health.data.value?.scheduler_running
        "
        @change="(v: any) => action('/api/automation', { paused: !v })"
      />
    </div>
    <RouterLink to="/settings">自动化设置</RouterLink>
  </section>
  <div class="overview-grid">
    <section class="panel">
      <div class="section-head">
        <h2>最近执行</h2>
        <RouterLink to="/executions">查看全部</RouterLink>
      </div>
      <div v-if="!runs.length" class="empty">
        还没有执行记录。同步课程后，系统会自动安排处理。
      </div>
      <button
        type="button"
        v-for="run in runs.slice(0, 6)"
        :key="run.id"
        class="run-row"
        @click="executionDetail = run.id"
      >
        <MaterialIcon
          class="run-symbol"
          :name="
            run.kind === 'live'
              ? 'videocam'
              : run.kind === 'sync'
                ? 'sync'
                : run.kind.includes('slides')
                  ? 'dashboard'
                  : 'logs'
          "
        />
        <span class="run-body">
          <b>{{ kinds[run.kind] || run.kind }}</b
          ><small
            >{{ courseName(run.course_id) }} · {{ date(run.updated) }}</small
          >
        </span>
        <el-tag :type="tone(run.status)">{{
          statuses[run.status] || run.status
        }}</el-tag>
      </button>
    </section>

    <section class="panel">
      <div class="section-head">
        <h2>我的课程</h2>
        <RouterLink to="/files">浏览产物</RouterLink>
      </div>
      <div class="course-grid">
        <RouterLink
          v-for="c in courseRows.slice(0, 6)"
          :key="c.id"
          class="course-tile"
          :to="{ path: '/files', query: { course: c.id } }"
        >
          <h3>{{ c.name }}</h3>
          <span>{{ c.active ? "参与自动化" : "已停用" }} · {{ c.id }}</span>
        </RouterLink>
        <el-empty
          v-if="!courseRows.length"
          description="同步课表后，课程会显示在这里"
        />
      </div>
    </section>
  </div>
  <ExecutionDetail :id="executionDetail" @close="executionDetail = null" />
</template>
