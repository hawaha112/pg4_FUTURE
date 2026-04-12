const typeLabels = {"paper":"学术论文","news":"新闻报道","official":"官方发布","opinion":"观点文章","community":"社区讨论","video":"视频"};

// Lightweight Markdown → HTML renderer
function renderMd(text) {
    if (!text) return '';
    // Escape HTML first (prevent XSS), but preserve intentional markdown
    var s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // Tables: | col1 | col2 | ... (detect table blocks)
    s = s.replace(/((?:^|\n)\|.+\|(?:\n\|.+\|)+)/g, function(block) {
        var rows = block.trim().split('\n');
        var html = '<table class="md-table">';
        rows.forEach(function(row, i) {
            // Skip separator row (|---|---|)
            if (/^[\|\s\-:]+$/.test(row)) return;
            var cells = row.split('|').filter(function(c, j) { return j > 0 && j < row.split('|').length - 1; });
            var tag = (i === 0) ? 'th' : 'td';
            html += '<tr>' + cells.map(function(c) { return '<' + tag + '>' + c.trim() + '</' + tag + '>'; }).join('') + '</tr>';
        });
        return html + '</table>';
    });

    // Headers: ### text
    s = s.replace(/^### (.+)$/gm, '<h4 class="md-h">$1</h4>');
    s = s.replace(/^## (.+)$/gm, '<h3 class="md-h">$1</h3>');

    // Bold: **text**
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Horizontal rule: ---
    s = s.replace(/^---$/gm, '<hr class="md-hr">');

    // Unordered list blocks: consecutive lines starting with -
    s = s.replace(/((?:^|\n)- .+(?:\n- .+)*)/g, function(block) {
        var items = block.trim().split('\n').map(function(line) {
            return '<li>' + line.replace(/^- /, '') + '</li>';
        }).join('');
        return '<ul class="md-list">' + items + '</ul>';
    });

    // Paragraphs: double newline
    s = s.replace(/\n{2,}/g, '</p><p>');
    // Single newlines within a paragraph → <br> (but not after block elements)
    s = s.replace(/([^>])\n([^<])/g, '$1<br>$2');

    // Wrap in paragraph tags
    s = '<p>' + s + '</p>';
    // Clean up empty paragraphs
    s = s.replace(/<p>\s*<\/p>/g, '');
    // Don't wrap block elements in <p>
    s = s.replace(/<p>\s*(<(?:table|ul|h[34]|hr)[^>]*>)/g, '$1');
    s = s.replace(/(<\/(?:table|ul|h[34])>)\s*<\/p>/g, '$1');

    return s;
}

function openModal(idx) {
    const a = __data[idx];
    if (!a) return;

    // Hero image (full-bleed top) —— 唯一保留的视觉锚点
    let heroHtml = '';
    if (a.image) {
        heroHtml = '<div class="m-hero"><img src="' + a.image + '" onerror="this.parentElement.style.display=&#39;none&#39;" alt=""><div class="m-hero-gradient"></div></div>';
    }

    // ⚠️ 刻意不再渲染 title / src-bar / why_it_matters：
    // 这些信息用户已经在卡片正面看过了，点开 modal 只应该看"新内容"。
    // 参考 html_generator.py 卡片正面：z1(分类·阅读时间) / card-title /
    // z2(key_details) / z3(why_it_matters) / z5(来源·时间) —— 上述全部不重复。

    // Detailed content — main reading section with markdown rendering
    let detailedHtml = '';
    if (a.detailed_content) {
        // 去掉 LLM 偶尔会生成的"### 问题背景/背景/前言/引言/简介"等背景类首节，
        // 避免和下方独立的"背景脉络"模块产生视觉重复。
        // 正则：匹配开头的 ### 小标题 + 本节内容，直到下一个 ### 或文末。
        let cleaned = a.detailed_content.replace(
            /^\s*###\s*(问题背景|背景|前言|引言|简介)[^\n]*\n[\s\S]*?(?=\n###\s|$)/,
            ''
        ).trim();
        // 兜底：如果清洗后全空（整段 detailed_content 就是一节背景），保留原文
        detailedHtml = '<div class="m-detailed">' + renderMd(cleaned || a.detailed_content) + '</div>';
    } else if (a.summary) {
        // 极端 fallback：没深度解读时才退到 summary
        detailedHtml = '<div class="m-detailed"><p>' + a.summary + '</p></div>';
    }

    // Article image gallery — extra images from the article page
    let galleryHtml = '';
    if (a.extra_images && a.extra_images.length > 0) {
        // Filter out the hero image to avoid duplicate
        let imgs = a.extra_images.filter(function(u) { return u !== a.image; });
        if (imgs.length > 0) {
            let imgTags = imgs.map(function(u) {
                return '<img src="' + u + '" onerror="this.style.display=&#39;none&#39;" alt="" loading="lazy">';
            }).join('');
            galleryHtml = '<div class="m-gallery">' + imgTags + '</div>';
        }
    }

    // Deep sections (background + analysis)
    let deepHtml = '';
    if (a.background) {
        deepHtml += '<div class="m-deep-section"><div class="m-deep-label">背景脉络</div><div class="m-deep-text">' + a.background + '</div></div>';
    }
    if (a.deep_analysis) {
        deepHtml += '<div class="m-deep-section"><div class="m-deep-label">深度解读</div><div class="m-deep-text">' + a.deep_analysis + '</div></div>';
    }
    if (deepHtml) {
        deepHtml = '<div class="m-deep">' + deepHtml + '</div>';
    }

    document.getElementById('modalContent').innerHTML =
        heroHtml +
        '<button class="m-close" onclick="closeModal()">✕</button>' +
        '<div class="m-body">' +
            detailedHtml +
            galleryHtml +
            deepHtml +
            '<div class="m-close-bottom" onclick="closeModal()">✕</div>' +
            '<div class="m-footer">' +
                '<span class="m-footer-src">' + a.reading_minutes + ' min read</span>' +
                '<a href="' + a.link + '" target="_blank" rel="noopener noreferrer" class="m-action" onclick="event.stopPropagation();">阅读原文 →</a>' +
            '</div>' +
        '</div>';

    document.getElementById('modalOverlay').classList.add('show');
    document.body.style.overflow = 'hidden';
    document.getElementById('modalOverlay').scrollTop = 0;
}

function closeModal() {
    document.getElementById('modalOverlay').classList.remove('show');
    document.body.style.overflow = '';
}

// Card click — event delegation with scroll vs tap detection
(function() {
    var grid = document.getElementById('grid');
    if (!grid) return;
    var touchStartX = 0, touchStartY = 0, touchStartTime = 0;
    var TAP_THRESHOLD = 10;  // px — finger movement within this = tap
    var TAP_MAX_MS = 300;    // max duration for a tap

    function openCard(card) {
        var idx = card.dataset.idx;
        if (idx !== undefined) openModal(parseInt(idx, 10));
    }

    // Desktop click — works fine, no scroll conflict
    grid.addEventListener('click', function(e) {
        // Ignore if touch device (handled by touch events below)
        if ('ontouchstart' in window) return;
        var card = e.target.closest('.card');
        if (card) openCard(card);
    });

    // Touch: record start position
    grid.addEventListener('touchstart', function(e) {
        if (e.touches.length === 1) {
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
            touchStartTime = Date.now();
        }
    }, {passive: true});

    // Touch: only open if finger barely moved (tap, not scroll)
    grid.addEventListener('touchend', function(e) {
        if (!e.changedTouches || e.changedTouches.length !== 1) return;
        var dx = e.changedTouches[0].clientX - touchStartX;
        var dy = e.changedTouches[0].clientY - touchStartY;
        var dt = Date.now() - touchStartTime;
        var dist = Math.sqrt(dx * dx + dy * dy);
        // Only count as tap if finger moved less than threshold and was quick
        if (dist < TAP_THRESHOLD && dt < TAP_MAX_MS) {
            var card = e.target.closest('.card');
            if (card) {
                e.preventDefault();
                openCard(card);
            }
        }
    }, {passive: false});
})();

// Filters
document.querySelectorAll('.f-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.f-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const f = btn.dataset.filter;
        document.querySelectorAll('.card').forEach(c => {
            c.classList.toggle('hidden', f !== 'all' && !(c.dataset.cat && c.dataset.cat.includes(f)));
        });
    });
});

// Search
document.getElementById('searchBox').addEventListener('input', function() {
    const q = this.value.toLowerCase();
    document.querySelectorAll('.card').forEach(c => {
        c.classList.toggle('hidden', q && !c.textContent.toLowerCase().includes(q));
    });
});

// Keyboard
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
        e.preventDefault();
        document.getElementById('searchBox').focus();
    }
});