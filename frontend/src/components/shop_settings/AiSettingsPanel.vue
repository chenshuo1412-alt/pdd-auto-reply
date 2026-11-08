<!--
  AI 设置面板（店铺管理「设置」弹窗内嵌，需求 8.6）
  职责：按店铺维度配置大语言模型参数（模型名称、API 密钥、API 地址、提示词指令、是否启用 AI）。
  说明：店铺由父级弹窗经 shopPk 传入；API 密钥后端不返回明文（需求 8.6），
        以占位提示「已配置（留空则不修改）」，仅在用户填写新值时提交。
-->
<script setup>
import { reactive, ref, computed, watch, onMounted } from 'vue'
import { Loading, Select } from '@/components/common'
import { showToast, TOAST_TYPE } from '@/utils/toast'
import { autoReplyApi } from '@/api'

const props = defineProps({
  shopPk: {
    type: [Number, String],
    default: '',
  },
})

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
// 获取模型列表状态
const fetchingModels = ref(false)
// 已获取的模型选项（{ id, name }），供下拉选择填充模型名称
const modelOptions = ref([])
// 模型下拉浮层是否展开
const showModelDropdown = ref(false)
// 是否按输入框内容过滤（手动输入时为 true，点击展开/选择后为 false）
const filterByInput = ref(false)
// 后端是否已配置过 API 密钥（用于占位提示）
const hasApiKey = ref(false)
// API 密钥是否明文显示（隐藏查看切换，与系统设置 SMTP 密码一致）
const showApiKey = ref(false)

// 接口类型选项（key + 中文 label + 默认地址），从后端字典加载
const providerOptions = ref([])
const providerSelectOptions = ref([])

const form = reactive({
  ai_enabled: false,
  provider_type: 'openai_compatible',
  model_name: '',
  api_base: '',
  api_key: '',
  instructions: '',
})

// 加载接口类型枚举（中文文案从后端数据字典查出，规范 15）
async function loadProviderTypes() {
  const data = await autoReplyApi.fetchAiProviderTypes().catch(() => null)
  const list = Array.isArray(data) ? data : []
  providerOptions.value = list
  providerSelectOptions.value = list.map((item) => ({
    value: item.key,
    label: item.label,
  }))
}

// 切换接口类型：若 API 地址为空，自动填入该类型默认地址（便于用户）
function onProviderChange() {
  // 不自动填入云端默认地址。地址留空时保存和测试都会被拒绝。
}

// 拉取某店铺 AI 配置
async function loadConfig() {
  if (!props.shopPk) {
    return
  }
  loading.value = true
  const data = await autoReplyApi.getAiConfig(props.shopPk).catch(() => null)
  loading.value = false
  // 未配置时后端返回 null，按默认空表单处理
  if (!data) {
    Object.assign(form, {
      ai_enabled: false,
      provider_type: 'openai_compatible',
      model_name: '',
      api_base: '',
      api_key: '',
      instructions: '',
    })
    hasApiKey.value = false
    return
  }
  Object.assign(form, {
    ai_enabled: data.ai_enabled === true,
    provider_type: data.provider_type || 'openai_compatible',
    model_name: data.model_name || '',
    api_base: data.api_base || '',
    // 反显已保存的密钥明文（支持隐藏查看）
    api_key: data.api_key || '',
    instructions: data.instructions || '',
  })
  hasApiKey.value = data.has_api_key === true
}

// 保存 AI 配置
async function save() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  // dashscope_app 用应用地址，无需模型名；其余启用 AI 时必须填写模型名称
  if (form.ai_enabled && form.provider_type !== 'dashscope_app' && !form.model_name.trim()) {
    showToast('启用 AI 时请填写模型名称', TOAST_TYPE.WARNING)
    return
  }
  if (!form.api_base.trim()) {
    showToast('请填写模型 API 地址，空白地址不会改用云端默认', TOAST_TYPE.WARNING)
    return
  }
  const payload = {
    ai_enabled: form.ai_enabled,
    provider_type: form.provider_type,
    model_name: form.model_name.trim() || null,
    api_base: form.api_base.trim() || null,
    instructions: form.instructions || null,
  }
  // 仅当用户填写了新密钥时才提交（留空表示不修改，避免覆盖已存密钥）
  if (form.api_key.trim()) {
    payload.api_key = form.api_key.trim()
  }
  saving.value = true
  const result = await autoReplyApi.saveAiConfig(props.shopPk, payload).catch(() => null)
  saving.value = false
  if (result === null) {
    return
  }
  showToast('保存成功', TOAST_TYPE.SUCCESS)
  // 重新拉取以刷新密钥占位状态
  loadConfig()
}

// 测试 AI 连接（密钥留空时后端回退使用已保存密钥）
async function testConnection() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  if (!form.api_base.trim()) {
    showToast('请填写模型 API 地址，空白地址不会改用云端默认', TOAST_TYPE.WARNING)
    testing.value = false
    return
  }
  testing.value = true
  const payload = {
    provider_type: form.provider_type,
    model_name: form.model_name.trim() || null,
    api_base: form.api_base.trim() || null,
  }
  if (form.api_key.trim()) {
    payload.api_key = form.api_key.trim()
  }
  const result = await autoReplyApi.testAiConfig(props.shopPk, payload).catch(() => null)
  testing.value = false
  if (result === null) {
    return
  }
  const reply = result && result.reply ? `，模型回复：${result.reply}` : ''
  showToast(`测试成功，AI 接口连接正常${reply}`, TOAST_TYPE.SUCCESS)
}

// 获取模型列表（自动获取模型名称）：密钥留空时后端回退使用已保存密钥
async function fetchModels() {
  if (!props.shopPk) {
    showToast('请先选择店铺', TOAST_TYPE.WARNING)
    return
  }
  if (!form.api_base.trim()) {
    showToast('请填写模型 API 地址，空白地址不会改用云端默认', TOAST_TYPE.WARNING)
    fetchingModels.value = false
    return
  }
  fetchingModels.value = true
  const payload = {
    provider_type: form.provider_type,
    api_base: form.api_base.trim() || null,
  }
  if (form.api_key.trim()) {
    payload.api_key = form.api_key.trim()
  }
  const result = await autoReplyApi.fetchAiModels(props.shopPk, payload).catch(() => null)
  fetchingModels.value = false
  if (result === null) {
    // 失败已由请求层统一弹出后端中文原因（可手动填写模型名称）
    return
  }
  const models = (result && result.models) || []
  modelOptions.value = models.map((m) => ({ id: m.id, name: m.name || m.id }))
  // 当前模型名为空或不在列表中时，默认选中第一个，便于快速确认
  if (modelOptions.value.length > 0 && !modelOptions.value.some((m) => m.id === form.model_name)) {
    form.model_name = modelOptions.value[0].id
  }
  // 获取成功后收起浮层，避免遮挡
  showModelDropdown.value = false
  filterByInput.value = false
  showToast(`获取模型列表成功，共 ${modelOptions.value.length} 个`, TOAST_TYPE.SUCCESS)
}

// 根据输入框内容过滤后的模型列表（输入时按 id/name 模糊匹配，展开时显示全部）
const filteredModels = computed(() => {
  const q = form.model_name.trim().toLowerCase()
  if (filterByInput.value && q) {
    return modelOptions.value.filter(
      (m) => m.id.toLowerCase().includes(q) || (m.name || '').toLowerCase().includes(q),
    )
  }
  return modelOptions.value
})

// 输入框输入时：标记按输入过滤，并展开浮层
function onModelInput() {
  filterByInput.value = true
  if (modelOptions.value.length > 0) {
    showModelDropdown.value = true
  }
}

// 输入框获焦时：若已有模型列表则展开浮层
function onModelFocus() {
  if (modelOptions.value.length > 0) {
    showModelDropdown.value = true
  }
}

// 输入框失焦时延迟收起，避免点击选项前浮层先消失
function onModelBlur() {
  window.setTimeout(() => {
    showModelDropdown.value = false
  }, 150)
}

// 点击右侧箭头：切换浮层展开状态并显示全部模型
function toggleModelDropdown() {
  filterByInput.value = false
  showModelDropdown.value = !showModelDropdown.value
}

// 从下拉选择模型时填充模型名称输入框并收起浮层
function onModelSelect(id) {
  form.model_name = id
  filterByInput.value = false
  showModelDropdown.value = false
}

// 切换接口类型后已获取的模型列表失效，清空避免误选
watch(
  () => form.provider_type,
  () => {
    modelOptions.value = []
    showModelDropdown.value = false
  },
)

onMounted(async () => {
  await loadProviderTypes()
  await loadConfig()
})
</script>

<template>
  <div class="panel">
    <p class="panel__desc">按店铺配置大语言模型参数，启用后在规则未命中时由 AI 结合知识库智能应答。</p>
    <div class="panel__body">
      <Loading :visible="loading" text="加载中..." />
      <label class="field field--inline">
        <input v-model="form.ai_enabled" type="checkbox" />
        <span class="field__label">启用 AI 智能回复</span>
      </label>
      <label class="field">
        <span class="field__label">接口类型</span>
        <Select
          v-model="form.provider_type"
          :options="providerSelectOptions"
          placeholder="请选择接口类型"
          @change="onProviderChange"
        />
      </label>
      <div class="field">
        <span class="field__label">模型名称</span>
        <div class="model-row">
          <div class="model-input-wrap">
            <input
              v-model="form.model_name"
              class="field__input model-input-wrap__input"
              type="text"
              placeholder="如：gpt-4o-mini / qwen-plus，或点击右侧自动获取"
              @input="onModelInput"
              @focus="onModelFocus"
              @blur="onModelBlur"
            />
            <button
              v-if="modelOptions.length"
              class="model-input-wrap__arrow"
              type="button"
              aria-label="展开模型列表"
              @mousedown.prevent="toggleModelDropdown"
            >
              <span class="model-input-wrap__arrow-icon" :class="{ 'is-open': showModelDropdown }">▾</span>
            </button>
            <div v-if="showModelDropdown && modelOptions.length" class="model-dropdown">
              <div v-if="filteredModels.length === 0" class="model-dropdown__empty">
                无匹配模型，将按当前输入保存
              </div>
              <div
                v-for="model in filteredModels"
                :key="model.id"
                class="model-dropdown__item"
                :class="{ 'is-active': form.model_name === model.id }"
                @mousedown.prevent="onModelSelect(model.id)"
              >
                <div class="model-dropdown__id">{{ model.id }}</div>
                <div v-if="model.name && model.name !== model.id" class="model-dropdown__name">
                  {{ model.name }}
                </div>
              </div>
            </div>
          </div>
          <button
            class="btn btn--default model-row__btn"
            type="button"
            :disabled="fetchingModels || !shopPk"
            @click="fetchModels"
          >
            {{ fetchingModels ? '获取中...' : '获取模型' }}
          </button>
        </div>
        <p class="model-hint">
          {{ modelOptions.length
            ? `已加载 ${modelOptions.length} 个模型，可直接选择或继续手动输入`
            : '可手动输入，或点击右侧按钮获取该服务商支持的模型列表' }}
        </p>
      </div>
      <label class="field">
        <span class="field__label">API 地址</span>
        <input v-model="form.api_base" class="field__input" type="text" placeholder="必填，例如本机 http://127.0.0.1:11434/v1。留空不会自动改用云端" />
      </label>
      <label class="field">
        <span class="field__label">API 密钥</span>
        <div class="apikey-wrap">
          <input
            v-model="form.api_key"
            class="field__input apikey-wrap__input"
            :type="showApiKey ? 'text' : 'password'"
            autocomplete="new-password"
            :placeholder="hasApiKey ? '已配置（留空则不修改）' : '请输入 API 密钥'"
          />
          <button
            class="apikey-wrap__toggle"
            type="button"
            :title="showApiKey ? '隐藏' : '显示'"
            @click="showApiKey = !showApiKey"
          >
            {{ showApiKey ? '隐藏' : '显示' }}
          </button>
        </div>
      </label>
      <label class="field">
        <span class="field__label">提示词指令</span>
        <textarea v-model="form.instructions" class="field__textarea" rows="4" placeholder="自定义 AI 回复的系统提示词（可空）"></textarea>
      </label>
      <div class="panel__actions">
        <button class="btn btn--default" type="button" :disabled="testing || !shopPk" @click="testConnection">
          {{ testing ? '测试中...' : '测试连接' }}
        </button>
        <button class="btn btn--primary" type="button" :disabled="saving || !shopPk" @click="save">
          {{ saving ? '保存中...' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
@import '@/components/shop_settings/panel.css';

.model-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}
.model-input-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
}
.model-input-wrap__input {
  width: 100%;
  padding-right: 32px;
  box-sizing: border-box;
}
.model-input-wrap__arrow {
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--color-text-secondary);
}
.model-input-wrap__arrow-icon {
  display: inline-block;
  transition: transform 0.15s ease;
  font-size: 12px;
  line-height: 1;
}
.model-input-wrap__arrow-icon.is-open {
  transform: rotate(180deg);
}
.model-dropdown {
  position: absolute;
  z-index: 20;
  top: calc(100% + 4px);
  left: 0;
  width: 100%;
  max-height: 240px;
  overflow: auto;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: 0 6px 18px rgba(15, 23, 42, 0.12);
}
.model-dropdown__empty {
  padding: 8px 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.model-dropdown__item {
  padding: 8px 12px;
  cursor: pointer;
}
.model-dropdown__item:hover {
  background: var(--color-hover-bg);
}
.model-dropdown__item.is-active {
  background: var(--color-primary-light);
}
.model-dropdown__id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
  color: var(--color-text);
}
.model-dropdown__name {
  margin-top: 2px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.model-row__btn {
  flex: 0 0 auto;
  white-space: nowrap;
}
.model-hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.apikey-wrap {
  position: relative;
  display: flex;
  width: 100%;
}
.apikey-wrap__input {
  width: 100%;
  padding-right: 56px;
  box-sizing: border-box;
}
.apikey-wrap__toggle {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  padding: 2px 8px;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 13px;
  color: var(--color-primary);
}
</style>
