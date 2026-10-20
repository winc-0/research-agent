<script setup lang="ts">
import { ref, onMounted, nextTick, computed } from 'vue'
import axios from 'axios'
import { marked } from 'marked'

// 后端固定 8000；用页面自身的 hostname，避免 localhost / 127.0.0.1 混用导致跨源
const API_BASE = `http://${location.hostname}:8000`
const WS_BASE = `ws://${location.hostname}:8000`

// Types
interface Message {
  role: 'user' | 'ai' | 'system'
  content: string
  logs?: LogItem[]
  files?: FileItem[]
  timestamp?: number
}

interface LogItem {
  type: string
  title: string
  details: any
  timestamp: string
}

interface FileItem {
  name: string
  path: string
  url: string
}

// State
const inputQuery = ref('')
const messages = ref<Message[]>([])
const status = ref<'idle' | 'running'>('idle')
const socket = ref<WebSocket | null>(null)
const currentSessionPath = ref('')
const currentSessionUrl = ref('')
const messagesEndRef = ref<HTMLElement | null>(null)
const isWelcomeScreen = computed(() => messages.value.length === 0)
const isSidebarOpen = ref(false)
const fileList = ref<any[]>([])
// 人工确认（HITL）：报告生成前先让用户过一遍大纲
const pendingConfirm = ref<any>(null)
const confirmFeedback = ref('')
const confirmSubmitting = ref(false)
// 生成一个持久的会话ID，如果页面不刷新，ID不变
const currentThreadId = ref(crypto.randomUUID())

// 起手示例：分别对应「数仓可答」「趋势类」「知识库可答」三类真实问题
const starters = [
  '2025 年各品类的出库货值排名如何',
  '2025 年各月出库单量有什么变化',
  '电动叉车的日常维护要点有哪些',
]

// Helper: Scroll to bottom
const scrollToBottom = async () => {
  await nextTick()
  if (messagesEndRef.value) {
    messagesEndRef.value.scrollIntoView({ behavior: 'smooth' })
  }
}

// Fetch Files
const fetchFiles = async () => {
  if (!currentSessionPath.value) return
  try {
    const res = await axios.get(`${API_BASE}/api/files`, {
      params: { path: currentSessionPath.value }
    })
    if (res.data.files) {
      fileList.value = res.data.files.map((f: any) => ({
        ...f,
        // 使用新的下载 API，传入绝对路径
        url: `${API_BASE}/api/download?path=${encodeURIComponent(f.path)}`
      }))
    }
  } catch (e) {
    console.error('Failed to fetch files', e)
  }
}

// WebSocket Connection
const connectWebSocket = () => {
  const ws = new WebSocket(`${WS_BASE}/ws/${currentThreadId.value}`)

  ws.onopen = () => {
    console.log('WebSocket Connected')
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      handleSocketMessage(data)
    } catch (e) {
      console.error('Error parsing WS message:', e)
    }
  }

  ws.onclose = () => {
    console.log('WebSocket Disconnected, retrying in 3s...')
    setTimeout(connectWebSocket, 3000)
  }

  socket.value = ws
}

// Handle Incoming Messages
const handleSocketMessage = (data: any) => {
  const { type, event, message, data: eventData } = data

  if (type === 'pong') return

  let lastAiMsg = messages.value.slice().reverse().find(m => m.role === 'ai')
  
  if (event === 'session_created') {
    currentSessionPath.value = eventData.path
    const parts = eventData.path.split(/output[\\/]/)
    if (parts.length > 1) {
      currentSessionUrl.value = `${API_BASE}/outputs/${parts[1].replace(/\\/g, '/')}`
    }
    isSidebarOpen.value = true
    fetchFiles()
  } else if (event === 'tool_start') {
    // 触发文件列表刷新，以确保用户能看到生成的文件
    if (currentSessionPath.value) {
      // 延迟一点刷新，因为工具刚开始运行，文件可能还没生成
      // 但如果是“写入文件”类工具，可能很快就有了
      // 这里可以尝试立即刷新 + 延迟刷新
      fetchFiles()
      setTimeout(fetchFiles, 2000)
    }

    if (lastAiMsg) {
      if (!lastAiMsg.logs) lastAiMsg.logs = []
      lastAiMsg.logs.push({
        type: 'tool',
        title: `调用工具：${eventData.tool_name}`,
        details: eventData.args,
        timestamp: new Date().toLocaleTimeString()
      })
      
      if (eventData.args && eventData.args.filename && currentSessionUrl.value) {
        if (!lastAiMsg.files) lastAiMsg.files = []
        const fileUrl = `${currentSessionUrl.value}/${eventData.args.filename}`
        // Avoid duplicates
        if (!lastAiMsg.files.find(f => f.name === eventData.args.filename)) {
           lastAiMsg.files.push({
            name: eventData.args.filename,
            path: eventData.args.filename,
            url: fileUrl
          })
        }
      }
    }
  } else if (event === 'assistant_call') {
    // 同样刷新文件列表
    if (currentSessionPath.value) {
        fetchFiles()
    }
     if (lastAiMsg) {
      if (!lastAiMsg.logs) lastAiMsg.logs = []
      lastAiMsg.logs.push({
        type: 'agent',
        title: `助手：${eventData.assistant_name}`,
        details: eventData.args,
        timestamp: new Date().toLocaleTimeString()
      })
    }
  } else if (event === 'task_result') {
    if (lastAiMsg) {
      lastAiMsg.content = eventData.result
    } else {
       messages.value.push({
        role: 'ai',
        content: eventData.result,
        timestamp: Date.now()
      })
    }
    pendingConfirm.value = null
    status.value = 'idle'
    fetchFiles()
  } else if (event === 'confirm_required') {
    // 图在这一步挂起，后端等 /api/confirm 的回调才会继续
    pendingConfirm.value = eventData
    if (lastAiMsg) {
      if (!lastAiMsg.logs) lastAiMsg.logs = []
      lastAiMsg.logs.push({
        type: 'info',
        title: `等待确认大纲（${(eventData.outline || []).length} 个部分）`,
        details: eventData.outline,
        timestamp: new Date().toLocaleTimeString()
      })
    }
  } else if (event === 'error') {
     messages.value.push({
      role: 'system',
      content: `出错了：${message}`,
      timestamp: Date.now()
    })
    status.value = 'idle'
  }
  
  scrollToBottom()
}

// Send Message
const sendMessage = async () => {
  if ((!inputQuery.value.trim() && selectedFiles.value.length === 0) || status.value === 'running') return

  const query = inputQuery.value
  inputQuery.value = ''
  status.value = 'running'

  messages.value.push({
    role: 'user',
    content: query,
    timestamp: Date.now()
  })

  messages.value.push({
    role: 'ai',
    content: '', // Start empty, show "Thinking" via logs/status if needed, or placeholder
    logs: [],
    files: [],
    timestamp: Date.now()
  })

  scrollToBottom()

  // Handle File Upload
  if (selectedFiles.value.length > 0) {
    console.log('Uploading files:', selectedFiles.value)
    
    // Log to UI
    const lastAiMsg = messages.value[messages.value.length - 1]
    if (lastAiMsg && lastAiMsg.role === 'ai') {
        if (!lastAiMsg.logs) lastAiMsg.logs = []
        
        const fileDetails = selectedFiles.value.map(f => ({ name: f.name, size: f.size }))
        
        lastAiMsg.logs.push({
            type: 'info',
            title: `正在上传 ${selectedFiles.value.length} 个文件…`,
            details: fileDetails,
            timestamp: new Date().toLocaleTimeString()
        })
    }

    // Actual Upload
    try {
        const formData = new FormData()
        // Ensure thread_id is available
        if (typeof currentThreadId !== 'undefined' && currentThreadId.value) {
             formData.append('thread_id', currentThreadId.value)
        } else {
             // Fallback if no thread ID (should ideally not happen as initialized in state)
             console.warn('No thread ID found for upload')
        }

        selectedFiles.value.forEach(file => {
            console.log(`Appending file to FormData: name=${file.name}, size=${file.size}, type=${file.type}`)
            formData.append('files', file)
        })

        await axios.post(`${API_BASE}/api/upload`, formData, {
            headers: {
                'Content-Type': 'multipart/form-data'
            }
        })
        
        // Clear files after successful upload
        selectedFiles.value = []
        
        if (lastAiMsg && lastAiMsg.logs) {
            lastAiMsg.logs.push({
                type: 'success',
                title: '文件上传成功',
                details: null,
                timestamp: new Date().toLocaleTimeString()
            })
        }

    } catch (e: any) {
        console.error('Upload failed', e)
        if (lastAiMsg && lastAiMsg.logs) {
            lastAiMsg.logs.push({
                type: 'error',
                title: '文件上传失败',
                details: e.message || 'Unknown error',
                timestamp: new Date().toLocaleTimeString()
            })
        }
        // Don't stop task execution, but maybe warn user?
    }
  }

  try {
    const payload: any = { query }
    // Only add thread_id if it exists and is not empty
    if (typeof currentThreadId !== 'undefined' && currentThreadId.value) {
      payload.thread_id = currentThreadId.value
    }
    console.log('Sending request payload:', payload)
    const res = await axios.post(`${API_BASE}/api/task`, payload)
    
    if (res.data && res.data.thread_id) {
      currentThreadId.value = res.data.thread_id
    }
  } catch (error: any) {
    console.error('Request failed:', error)
    let errorMsg = '请求发送失败。'
    if (error.message) errorMsg += ` (${error.message})`
    if (error.response && error.response.data) {
        errorMsg += ` Server says: ${JSON.stringify(error.response.data)}`
    }
    
    messages.value.push({
      role: 'system',
      content: errorMsg,
      timestamp: Date.now()
    })
    status.value = 'idle'
  }
}

// File Upload
const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFiles = ref<File[]>([])

const triggerFileUpload = () => {
  fileInputRef.value?.click()
}

const handleFileChange = (event: Event) => {
  const target = event.target as HTMLInputElement
  if (target.files && target.files.length > 0) {
    // Append new files to existing list
    selectedFiles.value = [...selectedFiles.value, ...Array.from(target.files)]
    console.log('Files selected:', selectedFiles.value)
    // Reset input so same file can be selected again if needed
    target.value = ''
  }
}

const removeFile = (index: number) => {
  selectedFiles.value.splice(index, 1)
}

const renderMarkdown = (text: string) => {
  if (!text) return '<span class="typing-indicator">正在思考…</span>'
  return marked(text)
}

// 提交人工确认：approve 直接写报告，revise 带着意见回到规划补检索
const submitConfirm = async (decision: 'approve' | 'revise') => {
  if (!pendingConfirm.value || confirmSubmitting.value) return
  if (decision === 'revise' && !confirmFeedback.value.trim()) return

  confirmSubmitting.value = true
  const feedback = confirmFeedback.value.trim()
  try {
    await axios.post(`${API_BASE}/api/confirm`, {
      thread_id: currentThreadId.value,
      decision,
      feedback,
    })
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'ai') {
      if (!last.logs) last.logs = []
      last.logs.push({
        type: 'success',
        title: decision === 'approve' ? '已确认大纲，开始撰写报告' : '已提交修改意见，重新检索',
        details: feedback ? { 意见: feedback } : null,
        timestamp: new Date().toLocaleTimeString()
      })
    }
    pendingConfirm.value = null
    confirmFeedback.value = ''
  } catch (e: any) {
    messages.value.push({
      role: 'system',
      content: `确认提交失败：${e?.message || e}`,
      timestamp: Date.now()
    })
  } finally {
    confirmSubmitting.value = false
  }
}

onMounted(() => {
  connectWebSocket()
})
</script>

<template>
  <div class="app-container">
    <!-- Main Content -->
    <main class="main-content" :class="{ 'centered-layout': isWelcomeScreen }">
      
      <!-- Sidebar Toggle Button -->
      <button 
        v-if="currentSessionPath && !isSidebarOpen" 
        class="sidebar-toggle-btn" 
        @click="isSidebarOpen = true"
        title="打开文件栏"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M4 6H20M4 12H20M4 18H20" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <!-- Welcome Screen -->
      <div v-if="isWelcomeScreen" class="welcome-screen">
        <div class="welcome-brand">
          <svg class="brand-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <defs>
              <linearGradient id="zxMarkHero" x1="4" y1="3" x2="28" y2="29" gradientUnits="userSpaceOnUse">
                <stop stop-color="#39E3AE"/>
                <stop offset="1" stop-color="#0C7F77"/>
              </linearGradient>
            </defs>
            <rect width="32" height="32" rx="9.5" fill="url(#zxMarkHero)"/>
            <path d="M16 6.2L19.6 16H12.4Z" fill="#04231E"/>
            <path d="M16 25.8L12.4 16H19.6Z" fill="#04231E" fill-opacity="0.4"/>
            <circle cx="16" cy="16" r="1.6" fill="#EAFFF9"/>
          </svg>
          <span class="brand-word">ZHI XUN<i>·</i>物流研究助手</span>
        </div>
        <div class="welcome-text">
          <h1>今天想查清什么？</h1>
          <p class="welcome-sub">
            我自己定计划、逐条找证据，卡住了会回头补检索，最后给你一份带引用的报告。
          </p>
        </div>
        <div class="starter-list">
          <button v-for="item in starters" :key="item" class="starter-chip" @click="inputQuery = item">
            {{ item }}
          </button>
        </div>
      </div>

      <!-- Chat Area -->
      <div v-else class="chat-scroll-area">
        <div class="chat-container">
          <div v-for="(msg, index) in messages" :key="index" class="message-wrapper" :class="msg.role">
            
            <!-- User Message -->
            <div v-if="msg.role === 'user'" class="message-user">
              <div class="msg-content">{{ msg.content }}</div>
            </div>

            <!-- AI Message -->
            <div v-else-if="msg.role === 'ai'" class="message-ai">
              <div class="ai-avatar">
                <svg width="28" height="28" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                  <defs>
                    <linearGradient id="zxMarkMsg" x1="4" y1="3" x2="28" y2="29" gradientUnits="userSpaceOnUse">
                      <stop stop-color="#39E3AE"/>
                      <stop offset="1" stop-color="#0C7F77"/>
                    </linearGradient>
                  </defs>
                  <rect width="32" height="32" rx="9.5" fill="url(#zxMarkMsg)"/>
                  <path d="M16 6.2L19.6 16H12.4Z" fill="#04231E"/>
                  <path d="M16 25.8L12.4 16H19.6Z" fill="#04231E" fill-opacity="0.4"/>
                  <circle cx="16" cy="16" r="1.6" fill="#EAFFF9"/>
                </svg>
              </div>
              
              <div class="ai-content-wrapper">
                <!-- Logs / Thinking Process -->
                <div v-if="msg.logs && msg.logs.length > 0" class="process-section">
                  <details>
                    <summary>
                      <span class="spinner" v-if="status === 'running' && index === messages.length - 1"></span>
                      查看研究过程
                    </summary>
                    <div class="process-steps">
                      <div v-for="(log, idx) in msg.logs" :key="idx" class="step-item">
                        <div class="step-header">
                          <span class="step-icon">🔧</span>
                          <span class="step-title">{{ log.title }}</span>
                        </div>
                        <div class="step-details" v-if="log.details">
                           <pre>{{ JSON.stringify(log.details, null, 2) }}</pre>
                        </div>
                      </div>
                    </div>
                  </details>
                </div>

                <!-- Text Content -->
                <div class="markdown-body" v-html="renderMarkdown(msg.content)"></div>

                <!-- Files -->
                <div v-if="msg.files && msg.files.length > 0" class="files-grid">
                  <a v-for="file in msg.files" :key="file.name" :href="file.url" target="_blank" class="file-card" :download="file.name">
                    <div class="file-icon">📄</div>
                    <div class="file-info">
                      <div class="file-name">{{ file.name }}</div>
                      <div class="file-type">文件</div>
                    </div>
                  </a>
                </div>
              </div>
            </div>

            <!-- System Message -->
             <div v-else class="message-system">
              {{ msg.content }}
            </div>

          </div>
          <div ref="messagesEndRef" class="spacer-bottom"></div>
        </div>
      </div>

      <!-- 人工确认：报告生成前的大纲确认 -->
      <div v-if="pendingConfirm" class="confirm-panel">
        <div class="confirm-header">
          <span class="confirm-title">确认研究大纲</span>
          <span class="confirm-sub">
            共 {{ (pendingConfirm.outline || []).length }} 个部分 ·
            {{ pendingConfirm.evidence_total || 0 }} 条证据
          </span>
        </div>
        <ul class="confirm-outline">
          <li v-for="(item, idx) in pendingConfirm.outline" :key="idx">
            <span class="confirm-index">{{ Number(idx) + 1 }}</span>
            <span class="confirm-question">{{ item['问题'] }}</span>
            <span class="confirm-meta">
              {{ (item['来源'] || []).join(' / ') }} · {{ item['证据数'] || 0 }} 条
            </span>
          </li>
        </ul>
        <textarea
          v-model="confirmFeedback"
          class="confirm-feedback"
          rows="2"
          placeholder="哪里还不够、还想看什么？写下意见后点「按意见补充检索」"
        ></textarea>
        <div class="confirm-actions">
          <button class="confirm-btn ghost" @click="submitConfirm('revise')"
                  :disabled="confirmSubmitting || !confirmFeedback.trim()">
            按意见补充检索
          </button>
          <button class="confirm-btn primary" @click="submitConfirm('approve')"
                  :disabled="confirmSubmitting">
            确认，生成报告
          </button>
        </div>
      </div>

      <!-- Input Area -->
      <footer class="input-footer">
        <!-- File Preview Tab -->
        <div v-if="selectedFiles.length > 0" class="file-preview-container">
          <div v-for="(file, index) in selectedFiles" :key="index" class="file-preview-chip">
            <span class="file-preview-icon">📎</span>
            <span class="file-preview-name">{{ file.name }}</span>
            <button class="file-remove-btn" @click="removeFile(index)" title="移除">×</button>
          </div>
        </div>

        <div class="input-container" :class="{ focused: status === 'running' }">
          <input 
            type="file" 
            ref="fileInputRef" 
            multiple
            style="display: none" 
            @change="handleFileChange" 
          />
          <button class="upload-btn" @click="triggerFileUpload" :disabled="status === 'running'" title="上传文件">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
          <textarea 
            v-model="inputQuery" 
            @keydown.enter.exact.prevent="sendMessage"
            placeholder="输入研究问题，例如：2025 年各品类的出库货值排名如何"
            :disabled="status === 'running'"
          ></textarea>
          <button class="send-btn" @click="sendMessage" :disabled="!inputQuery.trim() && status !== 'running'">
            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
              <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"></path>
            </svg>
          </button>
        </div>
        <div class="footer-text">
          物流研究助手会自主检索并生成带引用的报告，重要结论请自行核实。
        </div>
      </footer>
    </main>

    <!-- Right Sidebar (File Explorer) -->
    <aside v-if="isSidebarOpen" class="file-sidebar">
      <div class="sidebar-header">
        <h3>会话文件</h3>
        <div style="display: flex; gap: 8px; align-items: center;">
            <button class="folder-btn" @click="fetchFiles" title="刷新" style="padding: 4px 8px;">
                ↻
            </button>
            <button class="close-btn" @click="isSidebarOpen = false">×</button>
        </div>
      </div>
      <div class="file-list">
        <div v-if="fileList.length === 0" class="empty-files">
          还没有生成文件。
        </div>
        <div v-else v-for="file in fileList" :key="file.path" class="file-item">
          <a :href="file.url" target="_blank" class="file-link" :download="file.name">
            <span class="file-icon">📄</span>
            <span class="file-name-text">{{ file.name }}</span>
          </a>
        </div>
      </div>
    </aside>
  </div>
</template>

<style>
/* Global Resets & Variables */
:root {
  /* 墨青底：比原版更冷、更偏青绿，并留一点暖色做点缀 */
  --bg-deep: #08161A;
  --bg-glow: rgba(47, 214, 165, 0.10);
  --bg-glow-warm: rgba(242, 182, 92, 0.055);
  --surface: #102429;
  --surface-2: #16323A;
  --surface-3: #1E414B;
  --code-bg: #051013;
  --text-primary: #E4F2EF;
  --text-secondary: #9CB4B2;
  --text-muted: #65807F;
  --accent: #2FD6A5;
  --accent-deep: #0F8C74;
  --accent-soft: rgba(47, 214, 165, 0.14);
  --warm: #F2B65C;
  --user-msg-bg: #17414A;
  --border-color: #21414A;
}

body {
  margin: 0;
  background-color: var(--bg-deep);
  color: var(--text-primary);
  font-family: 'Inter', 'HarmonyOS Sans SC', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
  overflow: hidden; /* App handles scroll */
}

/* Layout */
.app-container {
  display: flex;
  height: 100vh;
  width: 100vw;
  background-color: var(--bg-deep);
  /* justify-content: center; Removed to allow sidebar layout */
}

/* Main Content */
.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  position: relative;
  background-color: var(--bg-deep);
  background-image:
    radial-gradient(1100px 560px at 50% -14%, var(--bg-glow), transparent 68%),
    radial-gradient(760px 460px at 108% 104%, var(--bg-glow-warm), transparent 72%);
  background-repeat: no-repeat;
  min-width: 0; /* Prevent flex overflow */
}

.sidebar-toggle-btn {
  position: absolute;
  top: 1rem;
  right: 1rem;
  background: transparent;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  z-index: 10;
  padding: 8px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}

.sidebar-toggle-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: var(--text-primary);
}

/* File Sidebar */
.file-sidebar {
  width: 300px;
  background-color: var(--surface);
  border-left: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}

.sidebar-header {
  padding: 1rem;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.sidebar-header h3 {
  margin: 0;
  font-size: 1rem;
  font-weight: 500;
  color: var(--text-primary);
}

.close-btn {
  background: none;
  border: none;
  color: var(--text-secondary);
  font-size: 1.5rem;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}

.close-btn:hover {
  color: var(--text-primary);
}

.file-list {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
}

.empty-files {
  color: var(--text-secondary);
  text-align: center;
  font-size: 0.9rem;
  margin-top: 2rem;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
}

.file-link {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem;
  border-radius: 8px;
  color: var(--text-primary);
  text-decoration: none;
  transition: background 0.2s;
  border: 1px solid transparent;
}

.file-link:hover {
  background: var(--surface-2);
  border-color: var(--border-color);
}

.folder-btn {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 0.5rem;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}

.folder-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: var(--text-primary);
  border-color: var(--text-secondary);
}

.file-name-text {
  font-size: 0.9rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Welcome Screen */
.welcome-screen {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  padding: 2rem;
}

/* Centered Layout Mode (Initial State) */
.main-content.centered-layout {
  justify-content: center;
  align-items: center;
  overflow-y: auto;
}

.main-content.centered-layout .welcome-screen {
  flex: 0 0 auto;
  padding-bottom: 2rem;
}

.main-content.centered-layout .input-footer {
  width: 100%;
  max-width: 100%;
  padding: 0;
  background: transparent;
  justify-content: center;
}

.welcome-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
}

.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  box-shadow: 0 8px 22px rgba(47, 214, 165, 0.26);
}

.brand-word {
  font-size: 0.76rem;
  letter-spacing: 0.2em;
  color: var(--text-secondary);
}

.brand-word i {
  font-style: normal;
  margin: 0 0.5em;
  color: var(--accent);
}

.welcome-text {
  text-align: center;
  margin-bottom: 1.75rem;
}

.welcome-text h1 {
  font-size: 2.35rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  background: linear-gradient(96deg, #E4F2EF 8%, #2FD6A5 62%, #F2B65C 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  margin: 0 0 0.7rem;
  line-height: 1.25;
}

.welcome-sub {
  margin: 0 auto;
  max-width: 32em;
  font-size: 0.95rem;
  line-height: 1.7;
  color: var(--text-secondary);
}

.starter-list {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0.6rem;
  max-width: 620px;
  padding: 0 1rem;
}

.starter-chip {
  background: var(--surface);
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 0.5rem 0.95rem;
  font-size: 0.82rem;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.18s;
}

.starter-chip:hover {
  color: var(--text-primary);
  border-color: var(--accent);
  background: var(--accent-soft);
}

/* Chat Area */
.chat-scroll-area {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
}

.chat-container {
  max-width: 800px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 2rem;
}

.message-wrapper {
  display: flex;
  flex-direction: column;
  width: 100%;
}

/* User Message */
.message-user {
  align-self: flex-end;
  max-width: 70%;
}

.msg-content {
  background-color: var(--user-msg-bg);
  padding: 12px 18px;
  border-radius: 18px;
  border-bottom-right-radius: 4px;
  line-height: 1.6;
}

/* AI Message */
.message-ai {
  align-self: flex-start;
  width: 100%;
  display: flex;
  gap: 1rem;
}

.ai-avatar {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  margin-top: 4px;
}

.ai-content-wrapper {
  flex: 1;
  min-width: 0; /* Text wrap fix */
}

.markdown-body {
  line-height: 1.6;
  font-size: 1rem;
}

.markdown-body pre {
  background: var(--surface-2);
  padding: 1rem;
  border-radius: 8px;
  overflow-x: auto;
}

.typing-indicator {
  color: var(--text-secondary);
  font-style: italic;
  animation: pulse 1.5s infinite;
}

@keyframes pulse {
  0% { opacity: 0.5; }
  50% { opacity: 1; }
  100% { opacity: 0.5; }
}

/* Process / Logs */
.process-section {
  margin-bottom: 1rem;
}

.process-section summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 0.85rem;
  list-style: none; /* Hide default arrow */
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem;
  border-radius: 4px;
}

.process-section summary:hover {
  background: var(--surface-2);
}

.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--text-secondary);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 1s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.process-steps {
  background: var(--surface);
  border-radius: 8px;
  padding: 0.5rem;
  margin-top: 0.5rem;
  border: 1px solid var(--border-color);
}

.step-item {
  padding: 0.5rem;
  border-left: 2px solid var(--border-color);
  margin-left: 0.5rem;
  margin-bottom: 0.5rem;
}

.step-header {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.step-details pre {
  margin: 0.5rem 0 0 0;
  font-size: 0.75rem;
  color: var(--text-muted);
  background: var(--code-bg);
  padding: 0.5rem;
  border-radius: 4px;
  overflow-x: auto;
}

/* Files Grid */
.files-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-top: 1rem;
}

.file-card {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  background: var(--surface-2);
  padding: 0.75rem 1rem;
  border-radius: 8px;
  text-decoration: none;
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  transition: all 0.2s;
  min-width: 150px;
}

.file-card:hover {
  background: var(--surface-3);
  border-color: var(--border-color);
}

.file-info {
  display: flex;
  flex-direction: column;
}

.file-name {
  font-weight: 500;
  font-size: 0.9rem;
}

.file-type {
  font-size: 0.75rem;
  color: var(--text-secondary);
}

/* System Message */
.message-system {
  text-align: center;
  font-size: 0.8rem;
  color: var(--text-muted);
  margin: 1rem 0;
}

.spacer-bottom { height: 100px; }

/* Input Footer */
.input-footer {
  background: transparent; /* 让主区的渐变透出来，避免出现一条硬边 */
  padding: 1rem 2rem 2rem 2rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
}

.input-container {
  width: 100%;
  max-width: 800px;
  background: var(--surface);
  border: 1px solid var(--border-color);
  border-radius: 28px;
  display: flex;
  align-items: center;
  padding: 0.45rem 0.7rem;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
  transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
}

.input-container.focused {
  background: var(--surface-2);
  border-color: var(--accent-deep);
  box-shadow: 0 0 0 3px var(--accent-soft), 0 10px 30px rgba(0, 0, 0, 0.32);
}

textarea {
  flex: 1;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 1rem;
  padding: 10px;
  resize: none;
  height: 24px;
  max-height: 200px;
  font-family: inherit;
  outline: none;
}

.send-btn {
  background: var(--accent);
  border: none;
  color: #04231E;
  cursor: pointer;
  padding: 8px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 14px rgba(47, 214, 165, 0.3);
  transition: all 0.18s;
}

.send-btn:disabled {
  background: transparent;
  color: var(--text-muted);
  box-shadow: none;
  cursor: default;
}

.send-btn:not(:disabled):hover {
  filter: brightness(1.07);
}

.upload-btn {
  background: none;
  border: none;
  color: var(--text-primary);
  cursor: pointer;
  padding: 8px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-right: 4px;
}

.upload-btn:hover {
  background: var(--surface-3);
}

.upload-btn:disabled {
  color: var(--text-muted);
  cursor: default;
}

.footer-text {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-align: center;
}

/* File Preview Styles */
.file-preview-container {
  width: 100%;
  max-width: 800px;
  display: flex;
  justify-content: flex-start;
  padding-left: 1rem;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.file-preview-chip {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: var(--surface-2);
  padding: 0.5rem 0.75rem;
  border-radius: 8px;
  border: 1px solid var(--border-color);
  font-size: 0.9rem;
  color: var(--text-primary);
  animation: slideUp 0.2s ease-out;
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.file-preview-icon {
  font-size: 1rem;
}

.file-preview-name {
  max-width: 200px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-remove-btn {
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 1.1rem;
  padding: 0 4px;
  line-height: 1;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.file-remove-btn:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #ff6b6b;
}

/* Scrollbar Styles */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
  background: var(--surface-3);
}

/* ---------- 人工确认面板 ---------- */
.confirm-panel {
  margin: 0 auto 12px;
  width: min(820px, calc(100% - 2rem));
  background: var(--surface);
  border: 1px solid var(--border-color);
  border-top: 2px solid var(--accent-deep);
  border-radius: 14px;
  padding: 14px 16px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
}

.confirm-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.confirm-title {
  font-size: 0.95rem;
  font-weight: 600;
  color: var(--text-primary);
}

.confirm-sub {
  font-size: 0.78rem;
  color: var(--text-secondary);
}

.confirm-outline {
  list-style: none;
  margin: 0 0 10px;
  padding: 0;
  max-height: 180px;
  overflow-y: auto;
}

.confirm-outline li {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 6px 0;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
  font-size: 0.86rem;
}

.confirm-outline li:last-child {
  border-bottom: none;
}

.confirm-index {
  flex: 0 0 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  border-radius: 50%;
  background: var(--surface-2);
  color: var(--text-secondary);
  font-size: 0.72rem;
}

.confirm-question {
  flex: 1;
  color: var(--text-primary);
  line-height: 1.45;
}

.confirm-meta {
  flex: 0 0 auto;
  color: var(--text-secondary);
  font-size: 0.75rem;
  white-space: nowrap;
}

.confirm-feedback {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  background: var(--surface-2);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  padding: 8px 10px;
  font-size: 0.85rem;
  font-family: inherit;
  outline: none;
}

.confirm-feedback:focus {
  border-color: var(--accent);
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 10px;
}

.confirm-btn {
  border-radius: 999px;
  padding: 7px 16px;
  font-size: 0.85rem;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.2s;
}

.confirm-btn.ghost {
  background: transparent;
  border-color: var(--border-color);
  color: var(--text-secondary);
}

.confirm-btn.ghost:hover:not(:disabled) {
  color: var(--text-primary);
  border-color: var(--text-secondary);
}

.confirm-btn.primary {
  background: var(--accent);
  color: #04231E;
  font-weight: 600;
}

.confirm-btn.primary:hover:not(:disabled) {
  filter: brightness(1.08);
}

.confirm-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
