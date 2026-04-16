const typeLabels = {"paper":"学术论文","news":"新闻报道","official":"官方发布","opinion":"观点文章","community":"社区讨论","video":"视频"};
const statusColors = {"official":"#8b5cf6","confirmed":"#10b981","reported":"#3b82f6","rumor":"#f59e0b"};

// ═══ Markdown → HTML renderer（带 heading id 注入）═══
var _headingCounter = 0;
function renderMd(text) {
    if (!text) return '';
    _headingCounter = 0;
    var s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // Tables
    s = s.replace(/((?:^|\n)\|.+\|(?:\n\|.+\|)+)/g, function(block) {
        var rows = block.trim().split('\n');
        var html = '<table class="md-table">';
        rows.forEach(function(row, i) {
            if (/^[\|\s\-:]+$/.test(row)) return;
            var cells = row.split('|').filter(function(c, j) { return j > 0 && j < row.split('|').length - 1; });
            var tag = (i === 0) ? 'th' : 'td';
            html += '<tr>' + cells.map(function(c) { return '<' + tag + '>' + c.trim() + '</' + tag + '>'; }).join('') + '</tr>';
        });
        return html + '</table>';
    });

    // Headers — 注入 id 用于 TOC 跳转
    s = s.replace(/^#### (.+)$/gm, function(m, t) { return '<h5 class="md-h md-h5" id="toc-' + (_headingCounter++) + '">' + t + '</h5>'; });
    s = s.replace(/^### (.+)$/gm, function(m, t) { return '<h4 class="md-h md-h4" id="toc-' + (_headingCounter++) + '">' + t + '</h4>'; });
    s = s.replace(/^## (.+)$/gm, function(m, t) { return '<h3 class="md-h md-h3" id="toc-' + (_headingCounter++) + '">' + t + '</h3>'; });

    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>');
    s = s.replace(/^---$/gm, '<hr class="md-hr">');

    // Lists
    s = s.replace(/((?:^|\n)\d+\. .+(?:\n\d+\. .+)*)/g, function(block) {
        return '<ol class="md-list md-ol">' + block.trim().split('\n').map(function(l) { return '<li>' + l.replace(/^\d+\. /, '') + '</li>'; }).join('') + '</ol>';
    });
    s = s.replace(/((?:^|\n)[-•] .+(?:\n[-•] .+)*)/g, function(block) {
        return '<ul class="md-list">' + block.trim().split('\n').map(function(l) { return '<li>' + l.replace(/^[-•] /, '') + '</li>'; }).join('') + '</ul>';
    });

    s = s.replace(/\n{2,}/g, '</p><p>');
    s = s.replace(/([^>])\n([^<])/g, '$1<br>$2');
    s = '<p>' + s + '</p>';
    s = s.replace(/<p>\s*<\/p>/g, '');
    s = s.replace(/<p>\s*(<(?:table|[uo]l|h[2-5]|hr)[^>]*>)/g, '$1');
    s = s.replace(/(<\/(?:table|[uo]l|h[2-5])>)\s*<\/p>/g, '$1');
    return s;
}

// ═══ 高亮关键数据 ═══
function highlightKeyData(html) {
    // 不在 HTML 标签内部做替换（避免破坏标签属性）
    return html.replace(/>([^<]+)</g, function(m, text) {
        var t = text;
        // 金额: $100M, $1.5B, ¥100亿, 100万, 1.5亿, 100亿美元
        t = t.replace(/(\$[\d,.]+\s*[MBKmkb](?:illion)?|¥[\d,.]+[万亿]?|[\d,.]+\s*[万亿](?:美元|元|人民币)?|[\d,.]+\s*(?:美元|USD|dollars?))/g,
            '<b class="hl-num">$1</b>');
        // 百分比: 18%, 提升18%, 增长30%
        t = t.replace(/([\d.]+\s*%)/g, '<b class="hl-num">$1</b>');
        // 大数字（4位以上纯数字，可能是参数量、用户数等）
        t = t.replace(/\b(\d{1,3}(?:,\d{3})+)\b/g, '<b class="hl-num">$1</b>');
        return '>' + t + '<';
    });
}

// ═══ 从已渲染 HTML 提取 TOC ═══
function buildToc(renderedHtml) {
    var toc = [];
    var re = /<h[3-5][^>]*id="(toc-\d+)"[^>]*>(.*?)<\/h[3-5]>/gi;
    var match;
    while ((match = re.exec(renderedHtml)) !== null) {
        var tag = match[0].charAt(2); // '3','4','5'
        toc.push({ id: match[1], text: match[2].replace(/<[^>]+>/g, ''), level: parseInt(tag) });
    }
    return toc;
}

// ═══ Lazy-load modal_data.js ═══
// 首屏只保证能渲染卡片；真正点开第一张卡时再保证 __data 就位，
// 避免把 130 KB+ 的 modal_data.js 塞进首屏关键路径。
var __dataLoading = false;
function ensureModalData(cb) {
    if (typeof __data !== 'undefined' && __data && __data.length) { cb(); return; }
    if (__dataLoading) {
        var waited = 0;
        var t = setInterval(function() {
            waited += 50;
            if (typeof __data !== 'undefined' && __data) { clearInterval(t); cb(); }
            else if (waited > 5000) { clearInterval(t); console.error('modal_data.js timeout'); }
        }, 50);
        return;
    }
    __dataLoading = true;
    var s = document.createElement('script');
    s.src = 'modal_data.js';
    s.onload = function() { cb(); };
    s.onerror = function() { console.error('failed to load modal_data.js'); };
    document.head.appendChild(s);
}

// ═══ Feedback helpers (localStorage) ═══
var _FB_KEY = 'ai_briefing_feedback_v1';
function getFeedback() {
    try { return JSON.parse(localStorage.getItem(_FB_KEY) || '{}'); } catch(e) { return {}; }
}
function setFeedback(itemId, value) {
    var fb = getFeedback();
    if (value === null) { delete fb[itemId]; } else { fb[itemId] = {v: value, t: Date.now()}; }
    try { localStorage.setItem(_FB_KEY, JSON.stringify(fb)); } catch(e) {}
}

// ═══ TOC sidebar active-section highlighting (scroll spy) ═══
var _tocSpyObserver = null;
function attachTocSpy() {
    if (_tocSpyObserver) { try { _tocSpyObserver.disconnect(); } catch(e) {} _tocSpyObserver = null; }
    var sidebar = document.querySelector('.m-toc-sidebar');
    if (!sidebar) return;
    var modalRoot = document.getElementById('modalOverlay');
    var links = sidebar.querySelectorAll('a[href^="#toc-"]');
    if (!links.length) return;
    // Use scroll event on the modal overlay (it's what scrolls)
    function update() {
        var best = null, bestTop = -Infinity;
        links.forEach(function(a) {
            var id = a.getAttribute('href').slice(1);
            var el = document.getElementById(id);
            if (!el) return;
            var top = el.getBoundingClientRect().top - 100;
            if (top <= 0 && top > bestTop) { bestTop = top; best = a; }
        });
        links.forEach(function(a) { a.classList.remove('active'); });
        if (best) best.classList.add('active');
    }
    modalRoot.addEventListener('scroll', update, {passive: true});
    update();
}

// ═══ Modal ═══
function openModal(idx) {
    ensureModalData(function() { _openModalReal(idx); });
}
function _openModalReal(idx) {
    const a = __data[idx];
    if (!a) return;

    // Hero image
    let heroHtml = '';
    if (a.image) {
        heroHtml = '<div class="m-hero"><img src="' + a.image + '" onerror="this.parentElement.style.display=\'none\'" alt=""><div class="m-hero-gradient"></div></div>';
    }

    // Title
    let titleHtml = '<h2 class="m-title">' + (a.chinese_title || a.title || '') + '</h2>';

    // Source bar: tier badge + icon + name + pub_date + source_type
    let srcPrefix = '';
    if (a.source_badge_icon && a.source_badge_label) {
        srcPrefix = '<span class="tier-badge tier-' + a.source_badge_label + '">' +
                    a.source_badge_icon + ' ' + a.source_badge_label + '</span> ';
    }
    let srcParts = [srcPrefix + (a.source_icon || '') + ' ' + (a.source_name || '')];
    if (a.pub_date) srcParts.push(a.pub_date);
    var tl = typeLabels[a.source_type];
    if (tl) srcParts.push(tl);
    let srcBarHtml = '<div class="m-src-bar">' + srcParts.join(' · ');
    if (a.event_status_icon && a.event_status_label) {
        var sc = statusColors[a.event_status] || '#888';
        srcBarHtml += ' · <span class="m-status-badge" style="color:' + sc + '">' + a.event_status_icon + ' ' + a.event_status_label + '</span>';
    }
    srcBarHtml += '</div>';

    // Multi-source indicator (moved here, right after source bar)
    let multiSrcHtml = '';
    if (a.also_reported_by && a.also_reported_by.length > 0) {
        multiSrcHtml = '<div class="m-multi-src"><span class="m-multi-src-label">' + a.report_count + ' 源报道</span>';
        a.also_reported_by.forEach(function(src) {
            multiSrcHtml += '<span class="m-multi-src-tag">' + src + '</span>';
        });
        multiSrcHtml += '</div>';
    }

    // ★ LEDE — why_it_matters + deep_analysis（置顶，让读者秒懂"为什么重要"） ★
    let ledeHtml = '';
    if (a.why_it_matters) {
        ledeHtml += '<div class="m-lede">' +
                    '<span class="m-lede-label">📌 为什么重要</span>' +
                    '<div class="m-lede-text"><strong>' + a.why_it_matters + '</strong></div>' +
                    '</div>';
    }
    if (a.deep_analysis) {
        ledeHtml += '<div class="m-lede">' +
                    '<span class="m-lede-label">🔍 深度解读</span>' +
                    '<div class="m-lede-text">' + renderMd(a.deep_analysis) + '</div>' +
                    '</div>';
    }
    // 原来的 whyHtml 占位改为不输出（已被 ledeHtml 包办）
    let whyHtml = '';

    // Detailed content
    let detailedHtml = '';
    let tocHtml = '';        // inline TOC（fallback / 窄屏）
    let tocSidebarHtml = ''; // 左侧悬浮 TOC（宽屏）
    if (a.detailed_content) {
        let cleaned = a.detailed_content.replace(
            /^\s*###\s*(问题背景|背景|前言|引言|简介)[^\n]*\n[\s\S]*?(?=\n###\s|$)/, ''
        ).trim();
        var rendered = renderMd(cleaned || a.detailed_content);
        rendered = highlightKeyData(rendered);

        // Build TOC from rendered HTML (reliable — uses actual id attributes)
        var toc = buildToc(rendered);
        if (toc.length >= 2) {
            var linksHtml = '';
            toc.forEach(function(h) {
                var cls = h.level >= 4 ? ' m-toc-sub' : '';
                linksHtml += '<a href="#' + h.id + '" class="m-toc-link' + cls + '" onclick="event.preventDefault();document.getElementById(\'' + h.id + '\').scrollIntoView({behavior:\'smooth\',block:\'start\'});return false;">' + h.text + '</a>';
            });
            // 两套：inline（窄屏/fallback）+ sidebar（宽屏悬浮）
            tocHtml = '<nav class="m-toc"><span class="m-toc-title">目录</span>' + linksHtml + '</nav>';
            tocSidebarHtml = '<nav class="m-toc-sidebar show" aria-label="目录导航">' +
                             '<div class="m-toc-sidebar-title">目录</div>' +
                             linksHtml + '</nav>';
        }
        detailedHtml = '<div class="m-detailed" id="m-detailed-area">' + rendered + '</div>';
    } else {
        // Fallback
        var parts = [];
        var mainText = (a.title && a.title.length > (a.summary || '').length + 20) ? a.title : (a.summary || '');
        if (mainText) parts.push('<p>' + mainText + '</p>');
        if (a.why_it_matters) parts.push('<div class="m-why">' + a.why_it_matters + '</div>');
        if (a.key_details && a.key_details.length > 0) {
            var kd = a.key_details.filter(function(k) { return k && k.length > 5; });
            if (kd.length > 0) {
                parts.push('<div class="m-keypoints"><div class="m-keypoints-label">要点</div><ul>' +
                    kd.map(function(k) { return '<li>' + k + '</li>'; }).join('') + '</ul></div>');
            }
        }
        if (parts.length > 0) detailedHtml = '<div class="m-detailed">' + parts.join('') + '</div>';
    }

    // Image gallery
    let galleryHtml = '';
    if (a.extra_images && a.extra_images.length > 0) {
        let imgs = a.extra_images.filter(function(u) { return u !== a.image; });
        if (imgs.length > 0) {
            galleryHtml = '<div class="m-gallery">' + imgs.map(function(u) {
                return '<img src="' + u + '" onerror="this.style.display=\'none\'" alt="" loading="lazy">';
            }).join('') + '</div>';
        }
    }

    // Deep sections（只保留 background；deep_analysis 已置顶到 lede）
    let deepHtml = '';
    if (a.background) deepHtml += '<div class="m-deep-section"><div class="m-deep-label">背景脉络</div><div class="m-deep-text">' + renderMd(a.background) + '</div></div>';
    if (deepHtml) deepHtml = '<div class="m-deep">' + deepHtml + '</div>';

    // Footer
    let footerAction = '';
    if (a.link && a.link !== '#') {
        footerAction = '<a href="' + a.link + '" target="_blank" rel="noopener noreferrer" class="m-action" onclick="event.stopPropagation();">阅读原文 →</a>';
    }

    // Feedback buttons — 👍/👎，localStorage 持久化
    var itemId = a.item_id || ('idx-' + idx);
    var _fbMap = getFeedback();
    var _fbCur = _fbMap[itemId] ? _fbMap[itemId].v : null;
    var upCls = _fbCur === 1 ? ' picked' : '';
    var downCls = _fbCur === -1 ? ' picked-bad' : '';
    var feedbackHtml =
        '<div class="m-feedback">' +
            '<div class="m-feedback-q">这条早报对你有用吗？</div>' +
            '<button class="m-feedback-btn' + upCls + '" data-fb="up" data-item="' + itemId + '" title="有用">👍</button>' +
            '<button class="m-feedback-btn' + downCls + '" data-fb="down" data-item="' + itemId + '" title="没用">👎</button>' +
        '</div>';

    // 先移除旧的 TOC sidebar（避免叠加）
    var existingToc = document.querySelector('.m-toc-sidebar');
    if (existingToc) existingToc.remove();

    document.getElementById('modalContent').innerHTML =
        heroHtml +
        '<button class="m-close" onclick="closeModal()">✕</button>' +
        '<div class="m-body">' +
            srcBarHtml +
            multiSrcHtml +
            titleHtml +
            ledeHtml +       // ← why_it_matters + deep_analysis 置顶
            tocHtml +        // inline（窄屏 fallback）
            detailedHtml +
            galleryHtml +
            deepHtml +
            feedbackHtml +   // ← 👍/👎 反馈
            '<div class="m-close-bottom" onclick="closeModal()">✕</div>' +
            '<div class="m-footer">' +
                '<span class="m-footer-src">' + a.reading_minutes + ' min read</span>' +
                footerAction +
            '</div>' +
        '</div>';

    // 插入左侧悬浮 TOC（宽屏）
    if (tocSidebarHtml) {
        document.getElementById('modalOverlay').insertAdjacentHTML('beforeend', tocSidebarHtml);
        // 在下次 paint 后启用滚动高亮
        requestAnimationFrame(attachTocSpy);
    }

    document.getElementById('modalOverlay').classList.add('show');
    document.body.style.overflow = 'hidden';
    document.getElementById('modalOverlay').scrollTop = 0;
}

function closeModal() {
    document.getElementById('modalOverlay').classList.remove('show');
    document.body.style.overflow = '';
    // 清理 TOC sidebar + scroll spy
    var tocSb = document.querySelector('.m-toc-sidebar');
    if (tocSb) tocSb.remove();
    if (_tocSpyObserver) { try { _tocSpyObserver.disconnect(); } catch(e) {} _tocSpyObserver = null; }
}

// ═══ Feedback button click (event delegation in modal) ═══
document.addEventListener('click', function(e) {
    var btn = e.target.closest('.m-feedback-btn');
    if (!btn) return;
    e.stopPropagation();
    var itemId = btn.dataset.item;
    if (!itemId) return;
    var fbMap = getFeedback();
    var cur = fbMap[itemId] ? fbMap[itemId].v : null;
    var val = btn.dataset.fb === 'up' ? 1 : -1;
    // 再次点击取消；否则覆盖
    var newVal = (cur === val) ? null : val;
    setFeedback(itemId, newVal);
    // 视觉反馈（兄弟按钮互斥）
    var parent = btn.parentElement;
    parent.querySelectorAll('.m-feedback-btn').forEach(function(b) {
        b.classList.remove('picked');
        b.classList.remove('picked-bad');
    });
    if (newVal === 1) btn.classList.add('picked');
    else if (newVal === -1) btn.classList.add('picked-bad');
});

// ═══ Card click (desktop + touch) ═══
(function() {
    // Event delegation for both grid and featured-grid
    function bindCardClick(container) {
        if (!container) return;
        var sx = 0, sy = 0, st = 0;
        container.addEventListener('click', function(e) {
            if ('ontouchstart' in window) return;
            var card = e.target.closest('[data-idx]');
            if (card) openModal(parseInt(card.dataset.idx, 10));
        });
        container.addEventListener('touchstart', function(e) {
            if (e.touches.length === 1) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; st = Date.now(); }
        }, {passive: true});
        container.addEventListener('touchend', function(e) {
            if (!e.changedTouches || e.changedTouches.length !== 1) return;
            var dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
            if (Math.sqrt(dx*dx+dy*dy) < 10 && Date.now() - st < 300) {
                var card = e.target.closest('[data-idx]');
                if (card) { e.preventDefault(); openModal(parseInt(card.dataset.idx, 10)); }
            }
        }, {passive: false});
    }
    bindCardClick(document.getElementById('grid'));
    bindCardClick(document.querySelector('.featured-grid'));
    bindCardClick(document.querySelector('.top3-grid'));
})();

// ═══ Filters (category AND audience AND search) ═══
var _activeCat = 'all';
var _activeAud = 'all';
var _activeQuery = '';

function _applyFilters() {
    var q = _activeQuery;
    document.querySelectorAll('.card,[class*="featured-card"]').forEach(function(c) {
        var cat = c.dataset.cat || '';
        var aud = c.dataset.aud || 'general';
        var catOk = (_activeCat === 'all') || cat.includes(_activeCat);
        var audOk = (_activeAud === 'all') || aud.split('|').indexOf(_activeAud) >= 0;
        var qOk   = !q || c.textContent.toLowerCase().includes(q);
        c.classList.toggle('hidden', !(catOk && audOk && qOk));
    });
}

document.querySelectorAll('.f-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
        document.querySelectorAll('.f-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        _activeCat = btn.dataset.filter;
        _applyFilters();
    });
});

document.querySelectorAll('.a-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
        document.querySelectorAll('.a-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        _activeAud = btn.dataset.audience;
        _applyFilters();
    });
});

// ═══ Search ═══
document.getElementById('searchBox').addEventListener('input', function() {
    _activeQuery = this.value.toLowerCase();
    _applyFilters();
});

// ═══ Keyboard ═══
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeModal();
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
        e.preventDefault();
        document.getElementById('searchBox').focus();
    }
});
