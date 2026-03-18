/* ═══════════════════════════════════════════════════════════
   Glass Tour — Onboarding tour engine
   Part of the Liquid Glass Design System v2

   Positioning strategy:
     - Spotlight wraps the target element exactly
     - Instruction panel is CENTERED on the page
     - Panel goes in the opposite half of the viewport from the element
     - If element is in the top half → panel in the bottom half
     - If element is in the bottom half → panel in the top half

   Persistence: uses localStorage key prefix 'udispute_tour'
   ═══════════════════════════════════════════════════════════ */

window.GlassTour = (function () {
  'use strict';

  var DEFAULT_KEY = 'udispute_tour_completed';
  var SPOTLIGHT_PAD = 12;
  var TOOLTIP_W = 420;
  var VIEWPORT_PAD = 24;

  var steps = [];
  var currentStep = 0;
  var backdrop, spotlight, tooltip;
  var isActive = false;
  var storageKey = DEFAULT_KEY;

  // ── Persistence ──
  function hasCompleted() {
    try { return localStorage.getItem(storageKey) === '1'; }
    catch (e) { return false; }
  }

  function markCompleted() {
    try { localStorage.setItem(storageKey, '1'); }
    catch (e) { /* silent */ }
  }

  function reset(key) {
    try {
      if (key) {
        localStorage.removeItem(key);
      } else {
        for (var i = localStorage.length - 1; i >= 0; i--) {
          var k = localStorage.key(i);
          if (k && k.indexOf('udispute_tour') === 0) {
            localStorage.removeItem(k);
          }
        }
      }
    } catch (e) { /* silent */ }
  }

  // ── Helpers ──
  function elPad(el) {
    var rect = el.getBoundingClientRect();
    return (rect.width < 200 || rect.height < 60) ? SPOTLIGHT_PAD : 0;
  }

  // Kill all CSS transitions on an element so repositioning is instant
  function freezeTransitions(el) {
    el.style.transition = 'none';
  }

  // Re-enable transitions for smooth reveal
  function unfreezeTransitions(el) {
    el.style.transition = '';
  }

  // ── Create DOM elements ──
  function createElements() {
    backdrop = document.createElement('div');
    backdrop.className = 'glass-tour-backdrop glass-tour-backdrop--entering';
    document.body.appendChild(backdrop);

    spotlight = document.createElement('div');
    spotlight.className = 'glass-tour-spotlight';
    spotlight.style.opacity = '0';
    document.body.appendChild(spotlight);

    tooltip = document.createElement('div');
    tooltip.className = 'glass-tour-tooltip';
    tooltip.style.opacity = '0';
    document.body.appendChild(tooltip);

    backdrop.addEventListener('click', function (e) {
      if (e.target === backdrop) next();
    });
  }

  function destroyElements() {
    if (backdrop) { backdrop.remove(); backdrop = null; }
    if (spotlight) { spotlight.remove(); spotlight = null; }
    if (tooltip) { tooltip.remove(); tooltip = null; }
  }

  // ── Position the spotlight around a target element (instant, no animation) ──
  function positionSpotlight(el) {
    var rect = el.getBoundingClientRect();
    var pad = elPad(el);

    spotlight.style.top = (rect.top - pad) + 'px';
    spotlight.style.left = (rect.left - pad) + 'px';
    spotlight.style.width = (rect.width + pad * 2) + 'px';
    spotlight.style.height = (rect.height + pad * 2) + 'px';
    spotlight.style.borderRadius = pad > 0 ? '16px' : (getComputedStyle(el).borderRadius || '24px');
    // NOTE: do NOT set opacity here — caller controls visibility
  }

  // ── Calculate where tooltip should go ──
  function calcTooltipPosition(el) {
    var rect = el.getBoundingClientRect();
    var pad = elPad(el);
    var vh = window.innerHeight;
    var vw = window.innerWidth;

    var left = (vw - TOOLTIP_W) / 2;
    left = Math.max(VIEWPORT_PAD, Math.min(left, vw - TOOLTIP_W - VIEWPORT_PAD));

    var elCenterY = rect.top + rect.height / 2;
    var spotlightBottom = rect.bottom + pad;
    var spotlightTop = rect.top - pad;

    return { left: left, elCenterY: elCenterY, spotlightBottom: spotlightBottom, spotlightTop: spotlightTop, vh: vh };
  }

  function calcTop(tooltipH, pos) {
    var top;
    if (pos.elCenterY < pos.vh / 2) {
      // Element in top half → tooltip in bottom half
      var minTop = pos.spotlightBottom + 20;
      var bottomSpace = pos.vh - minTop;
      top = minTop + Math.max(0, (bottomSpace - tooltipH) / 2);
      top = Math.min(top, pos.vh - tooltipH - VIEWPORT_PAD);
    } else {
      // Element in bottom half → tooltip in top half
      var maxBottom = pos.spotlightTop - 20;
      top = Math.max(VIEWPORT_PAD, (maxBottom - tooltipH) / 2);
      if (top + tooltipH > pos.spotlightTop - 20) {
        top = pos.spotlightTop - 20 - tooltipH;
      }
      top = Math.max(VIEWPORT_PAD, top);
    }
    return top;
  }

  // ── Scroll: put element in one half, leave other half for tooltip ──
  function scrollForStep(el) {
    var rect = el.getBoundingClientRect();
    var pad = elPad(el);
    var vh = window.innerHeight;
    var elAbsTop = window.scrollY + rect.top;
    var elH = rect.height + pad * 2;

    var targetViewportTop = vh * 0.08;
    if (elH > vh * 0.4) {
      targetViewportTop = 20;
    }

    var scrollTarget = elAbsTop - pad - targetViewportTop;
    scrollTarget = Math.max(0, scrollTarget);

    window.scrollTo({ top: scrollTarget, behavior: 'smooth' });
  }

  // ── Bind buttons inside the tooltip ──
  function bindButtons() {
    var btns = tooltip.querySelectorAll('[data-tour-action]');
    for (var i = 0; i < btns.length; i++) {
      (function (btn) {
        btn.addEventListener('click', function (e) {
          e.stopPropagation();
          e.preventDefault();
          var action = btn.getAttribute('data-tour-action');
          if (action === 'next') next();
          else if (action === 'skip') end();
        });
      })(btns[i]);
    }
  }

  // ── Render step content HTML ──
  function renderStep(index) {
    var step = steps[index];
    var total = steps.length;
    var isLast = index === total - 1;
    var isFirst = index === 0;

    var dotsHtml = '<div class="glass-tour-dots">';
    for (var i = 0; i < total; i++) {
      var cls = 'glass-tour-dot';
      if (i === index) cls += ' glass-tour-dot--active';
      else if (i < index) cls += ' glass-tour-dot--completed';
      dotsHtml += '<div class="' + cls + '"></div>';
    }
    dotsHtml += '</div>';

    var skipLabel = step.welcome ? 'Skip Tour' : 'Skip';
    var nextLabel = isLast ? 'Got It!' : (isFirst && step.welcome) ? "Let's Go" : step.interactive ? 'Try it \u2192' : 'Next';

    var skipBtn = '<button class="glass-tour-btn glass-tour-btn--skip" data-tour-action="skip">' + skipLabel + '</button>';
    var nextBtn = '<button class="glass-tour-btn glass-tour-btn--next" data-tour-action="next">' + nextLabel + '</button>';

    var interactiveBadge = step.interactive
      ? '<span class="glass-tour-interactive-badge">Click to try</span>'
      : '';

    return ''
      + '<div class="glass-tour-step-badge">Step ' + (index + 1) + ' of ' + total + '</div>'
      + '<div class="glass-tour-title">' + step.title + interactiveBadge + '</div>'
      + '<div class="glass-tour-body">' + step.body + '</div>'
      + '<div class="glass-tour-nav">'
      +   dotsHtml
      +   '<div class="glass-tour-btn-group">' + skipBtn + nextBtn + '</div>'
      + '</div>';
  }

  // ── Render a targeted step ──
  // Everything is positioned while invisible, then revealed in one frame
  function renderTargetedStep(index, target, step) {
    // 1. Freeze all transitions — no CSS animations during repositioning
    freezeTransitions(spotlight);
    freezeTransitions(tooltip);

    // 2. Hide both completely
    spotlight.style.opacity = '0';
    tooltip.style.opacity = '0';
    tooltip.style.transform = 'translateY(12px) scale(0.97)';

    // 3. Set tooltip content and class
    tooltip.className = 'glass-tour-tooltip';
    tooltip.innerHTML = renderStep(index);

    // 4. Position spotlight at target (invisible)
    positionSpotlight(target);

    // 5. Position tooltip — measure offscreen, then place
    var pos = calcTooltipPosition(target);
    tooltip.style.left = pos.left + 'px';
    tooltip.style.width = TOOLTIP_W + 'px';

    // Place offscreen to measure real height
    tooltip.style.top = '-9999px';

    // 6. Bind buttons now (content is in DOM)
    bindButtons();

    // 7. Wait one frame for layout, then position and reveal
    requestAnimationFrame(function () {
      var tooltipH = tooltip.offsetHeight;
      var top = calcTop(tooltipH, pos);

      // Set final position (still invisible, transitions still frozen)
      tooltip.style.top = top + 'px';

      // 8. Wait another frame so the browser registers the position
      //    THEN unfreeze transitions and reveal both together
      requestAnimationFrame(function () {
        // Unfreeze — CSS transitions now active for the opacity/transform reveal
        unfreezeTransitions(spotlight);
        unfreezeTransitions(tooltip);

        // Force reflow so the browser sees the frozen state before animating
        void spotlight.offsetHeight;

        // Reveal both simultaneously
        spotlight.style.opacity = '1';
        tooltip.style.opacity = '1';
        tooltip.style.transform = 'translateY(0) scale(1)';

        // ── Interactive step: make spotlight clickable ──
        if (step.interactive) {
          spotlight.classList.add('glass-tour-spotlight--interactive');
          spotlight.onclick = function (e) {
            e.stopPropagation();
            var href = target.getAttribute('href');
            if (href && href !== '#') {
              // Save resume state for cross-page navigation
              try {
                sessionStorage.setItem('glass_tour_resume', JSON.stringify({
                  key: storageKey,
                  step: index + 1
                }));
              } catch (ex) { /* silent */ }
              window.location.href = href;
            } else {
              // In-page action (button click, toggle, etc.)
              target.click();
              setTimeout(next, 600);
            }
          };
        } else {
          spotlight.classList.remove('glass-tour-spotlight--interactive');
          spotlight.onclick = null;
        }
      });
    });
  }

  // ── Show a step ──
  function showStep(index) {
    var step = steps[index];
    currentStep = index;

    if (step.welcome) {
      // ── Welcome modal — centered, no spotlight ──
      freezeTransitions(spotlight);
      spotlight.style.opacity = '0';

      tooltip.className = 'glass-tour-welcome';
      tooltip.style.opacity = '0';
      tooltip.style.transform = 'translate(-50%, -50%) scale(0.9)';
      tooltip.style.transition = 'none';
      tooltip.style.top = '';
      tooltip.style.left = '';
      tooltip.style.width = '';

      tooltip.innerHTML = ''
        + '<div class="glass-tour-welcome-icon">' + (index === 0 ? '&#x1F44B;' : '&#x1F680;') + '</div>'
        + renderStep(index);

      bindButtons();

      // Wait a frame, then reveal with animation
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          tooltip.style.transition = '';
          tooltip.style.opacity = '1';
          tooltip.style.transform = 'translate(-50%, -50%) scale(1)';
        });
      });

    } else {
      // ── Targeted step ──
      var target = document.querySelector(step.target);
      if (!target) {
        console.log('[GlassTour] Target not found, skipping:', step.target);
        if (index < steps.length - 1) {
          showStep(index + 1);
        } else {
          end();
        }
        return;
      }

      // Fixed-position elements don't need scrolling
      if (step.noScroll) {
        renderTargetedStep(index, target, step);
        return;
      }

      // Scroll element into position, then render after scroll settles
      scrollForStep(target);

      var scrollTimer;
      var renderDone = false;

      function doRender() {
        if (renderDone) return;
        renderDone = true;
        window.removeEventListener('scroll', onScrollEnd);
        renderTargetedStep(index, target, step);
      }

      function onScrollEnd() {
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(doRender, 150);
      }

      window.addEventListener('scroll', onScrollEnd);
      scrollTimer = setTimeout(doRender, 500);
    }
  }

  // ── Next step ──
  function next() {
    if (currentStep < steps.length - 1) {
      var nextIndex = currentStep + 1;

      // Fade out current tooltip
      unfreezeTransitions(tooltip);
      tooltip.style.opacity = '0';

      if (tooltip.classList.contains('glass-tour-welcome')) {
        tooltip.style.transform = 'translate(-50%, -50%) scale(0.95)';
      } else {
        tooltip.style.transform = 'scale(0.97)';
      }

      // Also fade spotlight
      unfreezeTransitions(spotlight);
      spotlight.style.opacity = '0';

      setTimeout(function () {
        showStep(nextIndex);
      }, 300);
    } else {
      end();
    }
  }

  // ── End tour ──
  function end() {
    isActive = false;
    markCompleted();

    if (backdrop) {
      backdrop.style.transition = 'opacity 0.4s ease';
      backdrop.style.opacity = '0';
    }
    if (spotlight) {
      unfreezeTransitions(spotlight);
      spotlight.style.opacity = '0';
    }
    if (tooltip) {
      unfreezeTransitions(tooltip);
      tooltip.style.opacity = '0';
    }

    setTimeout(destroyElements, 500);

    window.removeEventListener('resize', onResize);
    window.removeEventListener('keydown', onKeydown);
  }

  // ── Handle window resize ──
  function onResize() {
    if (!isActive) return;
    var step = steps[currentStep];
    if (step && !step.welcome && step.target) {
      var el = document.querySelector(step.target);
      if (el) {
        positionSpotlight(el);
        spotlight.style.opacity = '1';

        var pos = calcTooltipPosition(el);
        tooltip.style.left = pos.left + 'px';
        var tooltipH = tooltip.offsetHeight;
        tooltip.style.top = calcTop(tooltipH, pos) + 'px';
      }
    }
  }

  // ── Keyboard navigation ──
  function onKeydown(e) {
    if (!isActive) return;
    if (e.key === 'Escape') end();
    if (e.key === 'ArrowRight' || e.key === 'Enter') next();
  }

  // ── Public API ──
  function start(tourSteps, options) {
    options = options || {};

    storageKey = options.key || DEFAULT_KEY;

    if (!options.force && hasCompleted()) return;
    if (isActive) return;

    steps = tourSteps;
    currentStep = 0;
    isActive = true;

    createElements();

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        backdrop.classList.remove('glass-tour-backdrop--entering');
        backdrop.classList.add('glass-tour-backdrop--visible');
      });
    });

    // Check for cross-page resume from interactive step
    var resumeStep = 0;
    try {
      var resumeData = sessionStorage.getItem('glass_tour_resume');
      if (resumeData) {
        var parsed = JSON.parse(resumeData);
        if (parsed.key === storageKey && parsed.step < steps.length) {
          resumeStep = parsed.step;
        }
        sessionStorage.removeItem('glass_tour_resume');
      }
    } catch (ex) { /* silent */ }

    setTimeout(function () {
      showStep(resumeStep);
    }, 350);

    window.addEventListener('resize', onResize);
    window.addEventListener('keydown', onKeydown);
  }

  return {
    start: start,
    next: next,
    end: end,
    reset: reset,
    isActive: function () { return isActive; }
  };

})();
