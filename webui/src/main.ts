import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import { VueQueryPlugin } from "@tanstack/vue-query";
import {
  ElAlert,
  ElButton,
  ElCheckbox,
  ElDialog,
  ElDivider,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
} from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import "./style.css";
const router = createRouter({
  history: createWebHistory(),
  scrollBehavior: (_to, _from, savedPosition) => savedPosition || { top: 0 },
  routes: [
    { path: "/", component: () => import("./pages/OverviewPage.vue") },
    { path: "/running", component: () => import("./pages/RunningPage.vue") },
    { path: "/courses", component: () => import("./pages/CoursesPage.vue") },
    {
      path: "/executions",
      component: () => import("./pages/ExecutionsPage.vue"),
    },
    {
      path: "/classroom-events",
      component: () => import("./pages/ClassroomEventsPage.vue"),
    },
    { path: "/homework", component: () => import("./pages/HomeworkPage.vue") },
    { path: "/local", redirect: "/files" },
    { path: "/files", component: () => import("./pages/FilesPage.vue") },
    { path: "/settings", component: () => import("./pages/SettingsPage.vue") },
    { path: "/auth", component: () => import("./pages/AuthPage.vue") },
    { path: "/logs", component: () => import("./pages/LogsPage.vue") },
  ],
});
const app = createApp(App).use(router).use(VueQueryPlugin);
for (const component of [
  ElAlert,
  ElButton,
  ElCheckbox,
  ElDialog,
  ElDivider,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElOption,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
])
  app.component(component.name!, component);
app.mount("#app");
