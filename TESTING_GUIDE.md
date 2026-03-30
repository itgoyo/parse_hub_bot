# 测试文件 - 优化后的功能验证

## 1. 测试广告按钮服务

```bash
# 创建临时测试脚本
cd /Users/itgoyo/Documents/code/parse_hub_bot

cat > test_ad_service.py <<'EOF'
import asyncio
import sys
sys.path.insert(0, '.')

# 模拟ad.txt内容
class MockHttpxClient:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def get(self, url, timeout=None):
        class MockResp:
            text = """电报导航:https://dianbaodaohang.com
机场大全:https://vpnnav.github.io
搜索机器人:https://t.me/jiso?start=a_7202424896
搜索机器人:https://t.me/soso?start=a_7202424896"""
            def raise_for_status(self):
                pass
        return MockResp()

# Monkey patch
import services.ad as ad_mod
import httpx
original_client = httpx.AsyncClient
httpx.AsyncClient = MockHttpxClient

async def test():
    from services.ad import fetch_ads, get_random_ad
    
    ads = await fetch_ads()
    print(f"✅  获取到 {len(ads)} 条广告")
    for label, url in ads:
        print(f"   - {label}: {url}")
    
    random_ad = await get_random_ad()
    if random_ad:
        label, url = random_ad
        print(f"\n✅ 随机广告: {label} -> {url}")

asyncio.run(test())
EOF

uv run python test_ad_service.py && rm test_ad_service.py
```

## 2. 测试 Twitter Token 管理

```bash
cat > test_twitter_tokens.py <<'EOF'
import sys
import json
from pathlib import Path

sys.path.insert(0, '.')

# 创建临时配置路径
test_config_path = Path('./test_config')
test_config_path.mkdir(exist_ok=True)
test_tokens_file = test_config_path / 'twitter_tokens.json'

# Monkey patch
class MockBS:
    config_path = test_config_path

import services.twitter_tokens as tw_mod
tw_mod.bs = MockBS()
tw_mod.TOKENS_FILE = test_tokens_file

from services.twitter_tokens import TwitterTokenStore

store = TwitterTokenStore()

# 测试添加 token
print("=== 测试添加 Token ===")
is_new = store.add_token('auth_abc123', 'ct0_xyz789')
print(f"✅ 添加新 Token: {is_new}, 当前共 {store.count()} 个")

# 测试获取 cookie
print("\n=== 测试获取 Cookie ===")
cookie = store.get_cookie_str()
print(f"✅ Cookie: {cookie}")

# 测试添加第二个 token
is_new2 = store.add_token('auth_def456', 'ct0_uvw321')
print(f"\n✅ 添加第二个 Token: {is_new2}, 当前共 {store.count()} 个")

# 测试轮询
print("\n=== 测试轮询 ===")
for i in range(3):
    cookie = store.get_cookie_str()
    print(f"轮询 {i+1}: {cookie}")

# 测试删除
print("\n=== 测试删除 ===")
store.remove_token_by_auth('auth_abc123')
print(f"✅ 删除后剩余 {store.count()} 个")

# 清理
import shutil
shutil.rmtree(test_config_path)
print("\n✅ 测试完成并清理")
EOF

uv run python test_twitter_tokens.py && rm test_twitter_tokens.py
```

## 3. 测试 Twitter Token 从字符串解析和删除

```bash
cat > test_token_parsing.py <<'EOF'
import re
import sys
from pathlib import Path

sys.path.insert(0, '.')

# 创建临时配置路径
test_config_path = Path('./test_config')
test_config_path.mkdir(exist_ok=True)
test_tokens_file = test_config_path / 'twitter_tokens.json'

# Monkey patch
class MockBS:
    config_path = test_config_path

import services.twitter_tokens as tw_mod
tw_mod.bs = MockBS()
tw_mod.TOKENS_FILE = test_tokens_file

from services.twitter_tokens import TwitterTokenStore

store = TwitterTokenStore()
store.add_token('test_auth_token_123', 'test_ct0_456')

print("=== 测试从 Cookie 字符串删除 ===")
print(f"添加前: {store.count()} 个 Token")

cookie_str = "auth_token=test_auth_token_123; ct0=test_ct0_456"
store.remove_token_by_cookie_str(cookie_str)

print(f"删除后: {store.count()} 个 Token")
print("✅ Cookie 字符串解析和删除成功")

# 清理
import shutil
shutil.rmtree(test_config_path)
print("✅ 测试完成并清理")
EOF

uv run python test_token_parsing.py && rm test_token_parsing.py
```

## 4. 功能说明

### 新增功能 1: 广告按钮

- **文件**: `services/ad.py`
- **功能**: 
  - 从 GitHub 获取广告数据（1小时缓存）
  - 解析 `label:url` 格式
  - 随机返回一条广告
- **使用位置**: 所有成功解析后的消息下方显示一个推荐按钮

### 新增功能 2: Twitter Token 管理

- **文件**: `services/twitter_tokens.py`
- **存储**: `data/config/twitter_tokens.json`
- **功能**:
  - 用户可提交 Twitter cookie（`auth_token=xxx; ct0=xxx`）
  - 自动轮换多个 token
  - 失效 token 自动删除
  - 当所有 token 失效时提示用户输入
- **工作流程**:
  1. 用户发送 Twitter 链接
  2. 如果需要认证且无可用 token，显示获取教程
  3. 用户发送 cookie 后自动保存
  4. 解析时自动轮换使用可用 token
  5. Token 失效自动切换下一个

### 修改的文件

1. **services/parser.py**
   - 集成 TwitterTokenStore
   - Twitter 平台优先使用 platform_config cookie
   - 回退到 token store
   - 失效 token 自动删除并重试

2. **plugins/parse.py**
   - 导入 InlineKeyboardButton/Markup
   - 添加 `_get_ad_markup()` 和 `_send_ad_button()` 帮助函数
   - 添加 Twitter token 输入处理器
   - 在 `handle_parse` 中预解析 Twitter 链接检测认证错误
   - 所有成功发送路径后添加广告按钮

3. **services/__init__.py**
   - 导出新模块

## 5. 使用示例

### Twitter 解析新流程

**场景 1**: 无 token 时解析受限推文

```
用户: https://x.com/someone/status/123456 (受限推文)
机器人: [显示获取教程]
        Twitter Cookie 获取教程
        
        由于该推文需要登录才能查看，请提供您的 Twitter Cookie...
        
用户: auth_token=abc123; ct0=xyz789
机器人: Twitter Token 已保存 ✅
        当前共有 1 个可用 Token。
        现在可以重新发送 Twitter 链接进行解析。
        
用户: https://x.com/someone/status/123456
机器人: [成功解析并发送媒体]
        [显示广告按钮: 📢 推荐]
```

**场景 2**: 已有 token 时自动使用

```
用户: https://x.com/someone/status/123456 (受限推文)
机器人: [自动使用已保存的 token]
        [成功解析并发送媒体]
        [显示广告按钮: 📢 推荐]
```

**场景 3**: Token 失效自动切换

```
用户: https://x.com/someone/status/123456
机器人: [Token1 失效，自动删除]
        [切换到 Token2]
        [成功解析]
        [显示广告按钮]
```

### 广告按钮展示

所有成功解析后，用户会看到：

```
[解析的媒体内容]
[来源链接]

📢 推荐
[电报导航] <- 点击跳转
```

## 6. 测试清单

- [x] 语法检查通过
- [ ] 启动机器人测试
- [ ] 测试普通平台解析 + 广告按钮
- [ ] 测试 Twitter 公开推文解析
- [ ] 测试 Twitter 受限推文（无 token）
- [ ] 测试提交 Twitter token
- [ ] 测试 token 自动轮换
- [ ] 测试 token 失效删除
- [ ] 测试缓存场景的广告按钮

## 7. 配置要求

确保有 `.env` 文件：
```bash
bot_token=你的机器人token
api_id=你的api_id
api_hash=你的api_hash
```

## 8. 启动测试

```bash
cd /Users/itgoyo/Documents/code/parse_hub_bot
uv run python bot.py
```

## 9. 数据文件

- **广告缓存**: 内存中，1小时刷新
- **Twitter tokens**: `data/config/twitter_tokens.json`（自动创建）
