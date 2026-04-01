<template>
  <div class="task-queue-view">
    <a-page-header title="任务队列" subtitle="查看任务队列状态和执行历史">
      <!-- 操作栏 -->
      <a-card :bordered="false" class="stats-card">
        <a-space>
          <a-button type="primary" @click="refreshAll" :loading="loading">
            <template #icon><icon-refresh /></template>
            刷新
          </a-button>
          <a-tag :color="wsConnected ? 'green' : 'orange'" size="small">
            {{ wsConnected ? '实时连接' : '轮询模式' }}
          </a-tag>
          <a-popconfirm content="确定要清空队列吗？正在执行的任务不会被中断。" @ok="handleClearQueue">
            <a-button status="warning" :loading="clearingQueue">
              <template #icon><icon-delete /></template>
              清空队列
            </a-button>
          </a-popconfirm>
          <a-popconfirm content="确定要清空历史记录吗？" @ok="handleClearHistory">
            <a-button status="danger" :loading="clearingHistory">
              <template #icon><icon-close /></template>
              清空历史
            </a-button>
          </a-popconfirm>
        </a-space>
      </a-card>

      <a-spin :loading="loading" style="width: 100%">
        <!-- 队列状态概览 -->
        <a-card :bordered="false" class="stats-card" title="队列状态">
          <a-row :gutter="16">
            <a-col :xs="24" :sm="12" :md="6">
              <div class="stat-item">
                <div class="stat-title"><icon-tag /> 队列标签</div>
                <div class="stat-value">{{ queueStatus.tag || '默认队列' }}</div>
              </div>
            </a-col>
            <a-col :xs="24" :sm="12" :md="6">
              <div class="stat-item">
                <div class="stat-title">
                  <icon-check-circle-fill v-if="queueStatus.is_running" style="color: #00b42a" />
                  <icon-close-circle-fill v-else style="color: #f53f3f" />
                  运行状态
                </div>
                <div class="stat-value" :style="queueStatus.is_running ? { color: '#00b42a' } : { color: '#f53f3f' }">
                  {{ queueStatus.is_running ? '运行中' : '已停止' }}
                </div>
              </div>
            </a-col>
            <a-col :xs="24" :sm="12" :md="6">
              <a-statistic
                title="待执行任务"
                :value="queueStatus.pending_count ?? 0"
                :value-style="(queueStatus.pending_count ?? 0) > 0 ? { color: '#ff7d00' } : { color: '#00b42a' }"
              >
                <template #prefix>
                  <icon-clock-circle />
                </template>
              </a-statistic>
            </a-col>
            <a-col :xs="24" :sm="12" :md="6">
              <a-statistic title="历史记录数" :value="queueStatus.history_count ?? 0">
                <template #prefix>
                  <icon-history />
                </template>
              </a-statistic>
            </a-col>
          </a-row>
        </a-card>

        <!-- 当前执行任务 -->
        <a-card
          :bordered="false"
          class="stats-card"
          title="当前执行任务"
          v-if="queueStatus.current_task"
        >
          <a-descriptions :column="{ xs: 1, sm: 2, md: 3 }" bordered>
            <a-descriptions-item label="任务名称">
              {{ queueStatus.current_task.task_name }}
            </a-descriptions-item>
            <a-descriptions-item label="开始时间">
              {{ queueStatus.current_task.start_time }}
            </a-descriptions-item>
            <a-descriptions-item label="状态">
              <a-tag color="blue">{{ queueStatus.current_task.status }}</a-tag>
            </a-descriptions-item>
          </a-descriptions>
        </a-card>

        <!-- 待执行任务列表 -->
        <a-card
          :bordered="false"
          class="stats-card"
          title="待执行任务"
          v-if="queueStatus.pending_tasks && queueStatus.pending_tasks.length > 0"
        >
          <a-table
            :columns="pendingColumns"
            :data="queueStatus.pending_tasks"
            :pagination="{ pageSize: 10 }"
            :stripe="true"
            size="small"
          >
            <template #task_name="{ record }">
              <a-tag color="arcoblue">{{ record.task_name }}</a-tag>
            </template>
          </a-table>
        </a-card>

        <!-- 调度器状态 -->
        <a-card :bordered="false" class="stats-card" title="定时调度器">
          <a-descriptions :column="{ xs: 1, sm: 2, md: 3 }" bordered>
            <a-descriptions-item label="调度器状态">
              <a-tag :color="schedulerStatus.running ? 'green' : 'red'">
                {{ schedulerStatus.running ? '运行中' : '已停止' }}
              </a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="定时任务数">
              {{ schedulerStatus.job_count }}
            </a-descriptions-item>
          </a-descriptions>

          <!-- 定时任务列表 -->
          <a-table
            v-if="schedulerJobs.length > 0"
            :columns="schedulerColumns"
            :data="schedulerJobs"
            :pagination="{ pageSize: 10 }"
            :stripe="true"
            size="small"
            style="margin-top: 16px"
          >
            <template #next_run_time="{ record }">
              {{ record.next_run_time || '-' }}
            </template>
            <template #trigger="{ record }">
              <a-tooltip :content="record.trigger">
                <span style="cursor: pointer">{{ formatTrigger(record.trigger) }}</span>
              </a-tooltip>
            </template>
          </a-table>
        </a-card>

        <!-- 执行历史 -->
        <a-card :bordered="false" class="stats-card" title="执行历史（最近20条）">
          <a-table
            :columns="historyColumns"
            :data="queueStatus.recent_history || []"
            :pagination="{ pageSize: 10 }"
            :stripe="true"
            size="small"
          >
            <template #status="{ record }">
              <a-tag :color="getStatusColor(record.status)">
                {{ getStatusText(record.status) }}
              </a-tag>
            </template>
            <template #duration="{ record }">
              {{ record.duration ? `${record.duration}秒` : '-' }}
            </template>
            <template #error="{ record }">
              <a-tooltip v-if="record.error" :content="record.error">
                <span style="color: #f53f3f; cursor: pointer">{{ truncateError(record.error) }}</span>
              </a-tooltip>
              <span v-else>-</span>
            </template>
          </a-table>
        </a-card>
      </a-spin>
    </a-page-header>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { Message } from '@arco-design/web-vue'
import {
  IconRefresh,
  IconDelete,
  IconClose,
  IconTag,
  IconCheckCircleFill,
  IconCloseCircleFill,
  IconClockCircle,
  IconHistory,
} from '@arco-design/web-vue/es/icon'
import {
  getQueueStatus,
  clearQueue,
  clearHistory,
  getSchedulerStatus,
  getSchedulerJobs,
  type QueueStatus,
  type SchedulerStatus,
  type SchedulerJob,
} from '@/api/taskQueue'
import { getToken } from '@/utils/auth'

const loading = ref(false)
const clearingQueue = ref(false)
const clearingHistory = ref(false)
const wsConnected = ref(false)

const queueStatus = ref<QueueStatus>({
  tag: '',
  is_running: false,
  pending_count: 0,
  pending_tasks: [],
  current_task: null,
  history_count: 0,
  recent_history: [],
})

const schedulerStatus = ref<SchedulerStatus>({
  running: false,
  job_count: 0,
  next_run_times: [],
})

const schedulerJobs = ref<SchedulerJob[]>([])

// WebSocket 连接
let ws: WebSocket | null = null
let reconnectTimer: number | null = null
let refreshTimer: number | null = null

// 获取 WebSocket URL
const getWsUrl = () => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = window.location.host
  // API_BASE = /api/v1/wx
  const apiBase = '/api/v1/wx'
  const token = getToken()
  const tokenParam = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${protocol}//${host}${apiBase}/task-queue/ws${tokenParam}`
}

// 连接 WebSocket
const connectWebSocket = () => {
  if (ws) {
    ws.close()
  }

  try {
    const wsUrl = getWsUrl()
    ws = new WebSocket(wsUrl)

    ws.onopen = () => {
      wsConnected.value = true
      console.log('WebSocket 已连接')
      // 清除重连定时器
      if (reconnectTimer) {
        clearInterval(reconnectTimer)
        reconnectTimer = null
      }
    }

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data)
        if (message.type === 'queue_status' && message.data) {
          queueStatus.value = message.data
        }
      } catch (e) {
        console.error('解析 WebSocket 消息失败:', e)
      }
    }

    ws.onclose = () => {
      wsConnected.value = false
      console.log('WebSocket 已断开')
      // 自动重连
      if (!reconnectTimer) {
        reconnectTimer = window.setInterval(() => {
          if (!wsConnected.value) {
            connectWebSocket()
          }
        }, 5000)
      }
    }

    ws.onerror = (error) => {
      console.error('WebSocket 错误:', error)
      wsConnected.value = false
    }
  } catch (error) {
    console.error('WebSocket 连接失败:', error)
    wsConnected.value = false
  }
}

// 待执行任务表格列
const pendingColumns = [
  {
    title: '任务名称',
    dataIndex: 'task_name',
    slotName: 'task_name',
  },
  {
    title: '添加时间',
    dataIndex: 'add_time',
    width: '180px',
  },
]

// 定时任务表格列
const schedulerColumns = [
  {
    title: '任务ID',
    dataIndex: 'id',
    width: '150px',
  },
  {
    title: '触发器',
    dataIndex: 'trigger',
    slotName: 'trigger',
  },
  {
    title: '下次执行时间',
    dataIndex: 'next_run_time',
    slotName: 'next_run_time',
    width: '180px',
  },
]

// 执行历史表格列
const historyColumns = [
  {
    title: '任务名称',
    dataIndex: 'task_name',
    width: '120px',
  },
  {
    title: '开始时间',
    dataIndex: 'start_time',
    width: '180px',
  },
  {
    title: '结束时间',
    dataIndex: 'end_time',
    width: '180px',
  },
  {
    title: '耗时',
    dataIndex: 'duration',
    slotName: 'duration',
    width: '80px',
  },
  {
    title: '状态',
    dataIndex: 'status',
    slotName: 'status',
    width: '80px',
  },
  {
    title: '错误信息',
    dataIndex: 'error',
    slotName: 'error',
  },
]

// 获取状态颜色
const getStatusColor = (status: string) => {
  switch (status) {
    case 'completed':
      return 'green'
    case 'running':
      return 'blue'
    case 'failed':
      return 'red'
    default:
      return 'gray'
  }
}

// 获取状态文本
const getStatusText = (status: string) => {
  switch (status) {
    case 'completed':
      return '已完成'
    case 'running':
      return '执行中'
    case 'failed':
      return '失败'
    default:
      return status
  }
}

// 截断错误信息
const truncateError = (error: string) => {
  if (error.length > 30) {
    return error.substring(0, 30) + '...'
  }
  return error
}

// 格式化触发器显示
const formatTrigger = (trigger: string) => {
  if (!trigger) return '-'
  // 简化显示
  const parts = trigger.split(',')
  if (parts.length > 2) {
    return parts.slice(0, 2).join(',') + '...'
  }
  return trigger
}

// 加载调度器数据
const loadSchedulerData = async () => {
  try {
    const [schedulerData, jobsData] = await Promise.all([
      getSchedulerStatus(),
      getSchedulerJobs(),
    ])
    schedulerStatus.value = schedulerData
    schedulerJobs.value = jobsData.jobs || []
  } catch (error: any) {
    console.error('Load scheduler data error:', error)
  }
}

// 加载所有数据
const refreshAll = async () => {
  loading.value = true
  try {
    const [queueData, schedulerData, jobsData] = await Promise.all([
      getQueueStatus(),
      getSchedulerStatus(),
      getSchedulerJobs(),
    ])
    console.log('Queue data:', queueData)
    console.log('Scheduler data:', schedulerData)
    console.log('Jobs data:', jobsData)
    
    queueStatus.value = queueData
    schedulerStatus.value = schedulerData
    schedulerJobs.value = jobsData.jobs || []
  } catch (error: any) {
    console.error('Refresh error:', error)
    Message.error(error.message || '加载数据失败')
  } finally {
    loading.value = false
  }
}

// 清空队列
const handleClearQueue = async () => {
  clearingQueue.value = true
  try {
    await clearQueue()
    Message.success('队列已清空')
    await refreshAll()
  } catch (error: any) {
    Message.error(error.message || '清空队列失败')
  } finally {
    clearingQueue.value = false
  }
}

// 清空历史
const handleClearHistory = async () => {
  clearingHistory.value = true
  try {
    await clearHistory()
    Message.success('历史记录已清空')
    await refreshAll()
  } catch (error: any) {
    Message.error(error.message || '清空历史失败')
  } finally {
    clearingHistory.value = false
  }
}

// 自动刷新定时器（WebSocket 失败时的备用方案）
// refreshTimer 已在上方定义

onMounted(() => {
  // 立即加载所有数据
  refreshAll()
  // 尝试连接 WebSocket
  connectWebSocket()
  // 备用轮询（如果 WebSocket 未连接则使用）
  refreshTimer = window.setInterval(() => {
    if (!wsConnected.value) {
      refreshAll()
    }
  }, 10000)
})

onUnmounted(() => {
  if (ws) {
    ws.close()
    ws = null
  }
  if (reconnectTimer) {
    clearInterval(reconnectTimer)
    reconnectTimer = null
  }
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
})
</script>

<style scoped>
.task-queue-view {
  padding: 16px;
}

.stats-card {
  margin-bottom: 16px;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
  transition: all 0.3s ease;
}

.stats-card:hover {
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
}

.stat-item {
  text-align: center;
  padding: 8px 0;
}

.stat-title {
  font-size: 14px;
  color: var(--color-text-2);
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

.stat-value {
  font-size: 24px;
  font-weight: 500;
  color: var(--color-text-1);
}

:deep(.arco-statistic-title) {
  font-size: 14px;
  margin-bottom: 8px;
}

:deep(.arco-statistic-content) {
  font-size: 24px;
}

:deep(.arco-table-wrapper) {
  margin-top: -8px;
}
</style>
