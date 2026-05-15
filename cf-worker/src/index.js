// pg4-future-cron-trigger — Cloudflare Worker
//
// 用途: 用 CF Worker 自家的精准 cron 触发 GitHub Actions workflow_dispatch,
// 绕开 GH Actions schedule 整点队列拥挤导致的 30-90 分钟延迟.
//
// 触发链路:
//   CF Worker cron (UTC 22:00 / 10:00, 精准 ±30s)
//     → POST GH workflow_dispatch API
//       → GH Actions immediately starts (workflow_dispatch 几乎无队列)
//         → run_daily.sh 完整跑流水线
//           → TG 推送 + Pages 部署
//
// 调用失败时通过 TG bot 告警,不静默吞错.

const GH_REPO = 'hawaha112/pg4_FUTURE';
const WORKFLOW_FILE = 'morning-briefing.yml';

// cron 表达式 → 班次的映射(必须与 wrangler.toml 里的 crons 数组对齐)
const CRON_TO_SHIFT = {
  '0 22 * * *': 'am',   // UTC 22:00 = 北京 06:00 早班
  '0 10 * * *': 'pm',   // UTC 10:00 = 北京 18:00 晚班
};

export default {
  async scheduled(event, env, ctx) {
    const cronExpr = event.cron;
    const shift = CRON_TO_SHIFT[cronExpr] || 'auto';
    const triggeredAt = new Date().toISOString();

    console.log(`[${triggeredAt}] cron=${cronExpr} → shift=${shift}`);

    if (!env.GITHUB_PAT) {
      console.error('GITHUB_PAT secret 未配置, 无法触发 workflow');
      await sendTgAlert(env, `🚨 <b>CF Worker 配置错误</b>\nGITHUB_PAT secret 未设置,无法触发 GH workflow.`);
      return;
    }

    const url = `https://api.github.com/repos/${GH_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
    const body = {
      ref: 'main',
      inputs: {
        shift: shift,
        backfill_polluted_hours: '0',
        silent_tg: 'false',
      },
    };

    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.GITHUB_PAT}`,
          'Accept': 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'pg4-future-cron-trigger',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });

      if (resp.status === 204) {
        // GitHub workflow_dispatch 成功返回 204 No Content
        console.log(`✓ 已触发 ${WORKFLOW_FILE} shift=${shift} ref=main`);
        return;
      }

      const errBody = await resp.text();
      console.error(`触发失败: HTTP ${resp.status} body=${errBody.slice(0, 300)}`);
      await sendTgAlert(
        env,
        `🚨 <b>CF Worker 触发 GH workflow 失败</b>\n` +
        `班次: ${shift}\n` +
        `状态码: ${resp.status}\n` +
        `响应: <code>${escapeHtml(errBody.slice(0, 200))}</code>\n\n` +
        `请检查 GITHUB_PAT 权限或 workflow 是否存在.`
      );
    } catch (e) {
      console.error(`网络错误: ${e.message}`);
      await sendTgAlert(
        env,
        `🚨 <b>CF Worker 调 GH API 网络错误</b>\n` +
        `班次: ${shift}\n` +
        `错误: <code>${escapeHtml(e.message)}</code>`
      );
    }
  },

  // 可选: 通过 HTTP 访问 Worker URL 做手动测试
  // curl https://<your-worker>.workers.dev/?shift=am
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const shift = url.searchParams.get('shift') || 'auto';
    if (shift !== 'am' && shift !== 'pm' && shift !== 'auto') {
      return new Response('Invalid shift; use ?shift=am or pm or auto\n', { status: 400 });
    }

    if (!env.GITHUB_PAT) {
      return new Response('GITHUB_PAT secret missing\n', { status: 500 });
    }

    // 简单的 token 鉴权防止公网随便触发(可选, 用 Authorization: Bearer xxx)
    const authHeader = request.headers.get('Authorization') || '';
    const expectedToken = env.TRIGGER_TOKEN || '';
    if (expectedToken && authHeader !== `Bearer ${expectedToken}`) {
      return new Response('Unauthorized\n', { status: 401 });
    }

    const ghUrl = `https://api.github.com/repos/${GH_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
    const resp = await fetch(ghUrl, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_PAT}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'pg4-future-cron-trigger',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        ref: 'main',
        inputs: { shift, backfill_polluted_hours: '0', silent_tg: 'false' },
      }),
    });

    if (resp.status === 204) {
      return new Response(`✓ Triggered ${WORKFLOW_FILE} shift=${shift}\n`, { status: 200 });
    }
    const body = await resp.text();
    return new Response(`✗ HTTP ${resp.status}\n${body}\n`, { status: 502 });
  },
};

async function sendTgAlert(env, html) {
  if (!env.TG_BOT_TOKEN || !env.TG_CHAT_ID) {
    console.error('TG_BOT_TOKEN / TG_CHAT_ID 未配置, 跳过告警推送');
    return;
  }
  try {
    const resp = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: env.TG_CHAT_ID,
        text: html,
        parse_mode: 'HTML',
        disable_web_page_preview: true,
      }),
    });
    if (!resp.ok) {
      console.error(`TG 告警发送失败: HTTP ${resp.status}`);
    }
  } catch (e) {
    console.error(`TG 告警网络异常: ${e.message}`);
  }
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
