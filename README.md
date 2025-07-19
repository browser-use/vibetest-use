
<div align="right">
  <details>
    <summary >🌐 Language</summary>
    <div>
      <div align="center">
        <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=en">English</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=zh-CN">简体中文</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=zh-TW">繁體中文</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=ja">日本語</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=ko">한국어</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=hi">हिन्दी</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=th">ไทย</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=fr">Français</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=de">Deutsch</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=es">Español</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=it">Italiano</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=ru">Русский</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=pt">Português</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=nl">Nederlands</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=pl">Polski</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=ar">العربية</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=fa">فارسی</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=tr">Türkçe</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=vi">Tiếng Việt</a>
        | <a href="https://openaitx.github.io/view.html?user=browser-use&project=vibetest-use&lang=id">Bahasa Indonesia</a>
      </div>
    </div>
  </details>
</div>

# Vibetest Use

Automated QA testing using Browser-Use agents. 


https://github.com/user-attachments/assets/9558d051-78bc-45fd-8694-9ac80eaf9494


An MCP server that launches multiple Browser-Use agents to test a vibe-coded website for UI bugs, broken links, accessibility issues, and other technical problems.

Perfect for testing both live websites and localhost development sites. 

Vibecode and vibetest until your website works.

## Quick Start

```bash
# Install dependencies
uv venv
source .venv/bin/activate
uv pip install -e .
```

### 1) Claude Code

```bash
# Add MCP server via CLI
claude mcp add vibetest /full/path/to/vibetest-use/.venv/bin/vibetest-mcp -e GOOGLE_API_KEY="your_api_key"

# Test in Claude Code
> claude

> /mcp 
  ⎿  MCP Server Status

     • vibetest: connected
```

### 2) Cursor (manually)

1. **Install via MCP Settings UI:**
   - Open Cursor Settings
   - Click on "MCP" in the left sidebar  
   - Click "Add Server" or the "+" button
   - Manually edit config:
  
```json
{
  "mcpServers": {
    "vibetest": {
      "command": "/full/path/to/vibetest-use/.venv/bin/vibetest-mcp",
      "env": {
        "GOOGLE_API_KEY": "your_api_key"
      }
    }
  }
}

```

### Basic Prompts
```
> Vibetest my website with 5 agents: browser-use.com
> Run vibetest on localhost:3000
> Run a headless vibetest on localhost:8080 with 10 agents
```

### Parameters You Can Specify
- **URL**: Any website (`https://example.com`, `localhost:3000`, `http://dev.mysite.com`)
- **Number of agents**: `3` (default), `5 agents`, `2 agents` - more agents = more thorough testing
- **Headless mode**: `non-headless` (default) or `headless`

## Requirements

- Python 3.11+
- Google API key ([get one](https://developers.google.com/maps/api-security-best-practices)) (we support gemini-2.0-flash)
- Cursor/Claude with MCP support

## Full Demo


https://github.com/user-attachments/assets/6450b5b7-10e5-4019-82a4-6d726dbfbe1f



## License

MIT

---

Powered by [Browser Use](https://github.com/browser-use/browser-use) 
