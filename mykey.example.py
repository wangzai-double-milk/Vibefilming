# ════════════════════════════════════════════════════════════════════════════
#  VibeFilming — mykey 配置（复制为 mykey.py 后填入真实凭证）
#  cp mykey.example.py mykey.py
# ════════════════════════════════════════════════════════════════════════════
#
# 必填项：apikey + （可选）VOLC_AK / VOLC_SK
# apikey 不填就跑不起来；VOLC_AK/SK 不填整片 BGM 生成功能不可用，其余正常。
#
# 怎么拿 apikey（豆包 ARK，必填）：
#   1. 访问 https://console.volcengine.com/ark
#   2. 开通"豆包大模型 / Doubao"服务
#   3. 在 API Key 管理页新建一个 key（形如 ark-xxxxxxxx-xxxx-...）
#   4. 同账户下需开通这几个模型的访问权限：
#        · doubao-seed-2-0-pro-260215   （文本 + VLM + 视频理解，必需）
#        · doubao-seedream-4-5-251128   （文生图 / 图编辑，必需）
#        · doubao-seedance-2-0-260128   （文生视频，必需）
#
# 怎么拿 VOLC_AK / VOLC_SK（火山 OpenAPI，可选 - 用于 gen_audio_bgm 整片 BGM）：
#   1. 访问 https://console.volcengine.com/iam/keymanage 创建 AccessKey
#   2. 在 https://console.volcengine.com/ai-music 开通 BigMusic 服务（含纯音乐生成）
#   3. 给该 AK 所在子账号绑 BigMusicFullAccess 策略
#   4. 控制台拷贝出来的 SecretAccessKey 是 base64 形式（== 结尾），原样填即可
#

native_oai_config = {
    'name': 'doubao',
    'apikey': 'ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX',  # ← 填这里
    'apibase': 'https://ark.cn-beijing.volces.com/api/v3',
    'model': 'doubao-seed-2-0-pro-260215',
    'api_mode': 'chat_completions',
    'max_retries': 3,
    'connect_timeout': 10,
    'read_timeout': 120,
    'context_win': 24000,
}

mixin_config = {
    'llm_nos': ['doubao'],
    'max_retries': 5,
    'base_delay': 0.5,
}

# ============== 火山引擎 OpenAPI（BigMusic / GenBGM 整片 BGM 生成）==============
# 跟 ARK（Bearer token）不同，OpenAPI 用 AK/SK + HMAC-SHA256 V4 签名
# 不填这一段，gen_audio_bgm / query_audio_task 工具会被 stub，整片 BGM 不可用，
# 但 entity / shot 出片 / video_concat / amix 都可以正常跑（手动找现成 mp3 也行）
volc_open_api_config = {
    'VOLC_AK': 'AKLTXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX',
    'VOLC_SK': 'XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX==',
    'region':  'cn-beijing',
    'service': 'imagination',
}
