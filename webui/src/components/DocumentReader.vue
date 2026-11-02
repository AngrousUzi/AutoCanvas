<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { ElMessage } from "element-plus";
import { fileUrl } from "../api";
import { timecode, fileLabel, parseJsonLines } from "../workspace";
import type { OutputFile } from "../workspace";
import StructuredData from "./StructuredData.vue";
import SearchText from "./SearchText.vue";
const props = defineProps<{ file: OutputFile }>();
const search = ref(""),
  page = ref(1),
  mode = ref("timeline");
watch(
  () => props.file.path,
  () => {
    search.value = "";
    page.value = 1;
    mode.value = "timeline";
  },
);
watch(search, () => (page.value = 1));
const content = useQuery({
  queryKey: computed(() => ["document", props.file.path, props.file.modified]),
  queryFn: async ({ signal }) => {
    const response = await fetch(fileUrl(props.file.path), { signal });
    if (!response.ok) throw new Error("内容读取失败，请刷新后重试");
    return response.text();
  },
  enabled: computed(() => props.file.size <= 2 * 1024 * 1024),
});
const parsed = computed(() => {
  const text = content.data.value || "";
  if (!/\.jsonl?$/i.test(props.file.path)) return { value: null, warning: "" };
  try {
    if (props.file.path.endsWith(".json"))
      return { value: JSON.parse(text), warning: "" };
    const { rows, invalid } = parseJsonLines(text);
    return {
      value: rows,
      warning: invalid ? `${invalid} 条未完整写入的记录暂未展示。` : "",
    };
  } catch {
    return {
      value: null,
      warning: "结构化内容暂时无法解析，可阅读原文或下载。",
    };
  }
});
const segments = computed<{ start: number; end: number; text: string }[]>(
  () => {
    const value = parsed.value.value;
    if (
      Array.isArray(value) &&
      value.length &&
      value.every(
        (r) => r && typeof r.text === "string" && typeof r.start === "number",
      )
    )
      return value;
    if (props.file.path.endsWith(".txt")) {
      const rows = (content.data.value || "")
        .split("\n")
        .map((line) => line.match(/^\[(\d+):(\d+):(\d+)\]\s*(.*)$/));
      if (rows.some(Boolean))
        return rows.filter(Boolean).map((r) => ({
          start: Number(r![1]) * 3600 + Number(r![2]) * 60 + Number(r![3]),
          end: 0,
          text: r![4] || "",
        }));
    }
    return [];
  },
);
const filtered = computed(() =>
  segments.value.filter((s) =>
    s.text.toLowerCase().includes(search.value.toLowerCase()),
  ),
);
const text = computed(() =>
  segments.value.length
    ? segments.value.map((s) => s.text).join("\n")
    : content.data.value || "",
);
const paragraphs = computed(() =>
  text.value
    .split(/\n+/)
    .filter(
      (t) => t.trim() && t.toLowerCase().includes(search.value.toLowerCase()),
    ),
);
const pageRows = computed(() =>
  mode.value === "timeline" && segments.value.length
    ? filtered.value
    : paragraphs.value,
);
const pageCount = computed(() =>
  Math.max(1, Math.ceil(pageRows.value.length / 60)),
);
watch(mode, () => (page.value = 1));
watch(pageCount, (n) => (page.value = Math.min(page.value, n)));
async function copy() {
  try {
    await navigator.clipboard.writeText(text.value);
    ElMessage.success("已复制文字");
  } catch {
    ElMessage.error("无法访问剪贴板，可下载文字稿");
  }
}
</script>
<template>
  <div class="reader">
    <div class="reader-toolbar">
      <div>
        <b>{{ fileLabel(file) }}</b
        ><small v-if="segments.length"
          >{{ segments.length }} 段 ·
          {{ text.length.toLocaleString() }} 字</small
        >
      </div>
      <div class="inline-actions">
        <el-button v-if="content.data.value" link @click="copy"
          >复制文字</el-button
        ><a :href="fileUrl(file.path, true)" class="button-link"
          >下载原文件 ↗</a
        >
      </div>
    </div>
    <div v-if="file.size > 2 * 1024 * 1024" class="empty-state">
      <h3>这份内容较长</h3>
      <p>超过 2 MB，请下载阅读完整内容。</p>
    </div>
    <div v-else-if="content.isPending.value" class="empty-state">
      正在打开内容…
    </div>
    <div v-else-if="content.isError.value" class="empty-state">
      <h3>暂时无法读取内容</h3>
      <el-button @click="content.refetch()">重新加载</el-button>
    </div>
    <template v-else>
      <el-alert
        v-if="parsed.warning"
        :title="parsed.warning"
        type="info"
        :closable="false"
      />
      <template v-if="segments.length || parsed.value === null">
        <div class="reader-controls">
          <el-input
            v-model="search"
            clearable
            placeholder="在文字中搜索…"
            aria-label="搜索文字内容"
          />
          <div v-if="segments.length" class="segmented">
            <button
              :class="{ selected: mode === 'timeline' }"
              @click="mode = 'timeline'"
            >
              时间轴</button
            ><button
              :class="{ selected: mode === 'reading' }"
              @click="mode = 'reading'"
            >
              连续阅读
            </button>
          </div>
        </div>
        <p v-if="search" class="muted">找到 {{ pageRows.length }} 处匹配</p>
        <div
          v-if="mode === 'timeline' && segments.length"
          class="transcript-timeline"
        >
          <div
            v-for="(segment, i) in filtered.slice((page - 1) * 60, page * 60)"
            :key="i"
            class="transcript-segment"
          >
            <time>{{ timecode(segment.start) }}</time>
            <p>
              <SearchText
                :text="segment.text || '（此片段无识别文字）'"
                :query="search"
              />
            </p>
          </div>
        </div>
        <article v-else class="reading-text">
          <p
            v-for="(paragraph, i) in paragraphs.slice(
              (page - 1) * 60,
              page * 60,
            )"
            :key="i"
          >
            <SearchText :text="paragraph" :query="search" />
          </p>
        </article>
        <div v-if="!pageRows.length" class="empty-state">
          <p>
            {{
              search
                ? "没有匹配的文字，换个关键词试试。"
                : "这份文档暂无文字内容。"
            }}
          </p>
        </div>
        <div v-if="pageCount > 1" class="pagination-bar">
          <span>第 {{ page }} / {{ pageCount }} 页</span>
          <div>
            <el-button :disabled="page === 1" @click="page--">上一页</el-button
            ><el-button :disabled="page === pageCount" @click="page++"
              >下一页</el-button
            >
          </div>
        </div>
      </template>
      <StructuredData v-else :value="parsed.value" />
    </template>
  </div>
</template>
