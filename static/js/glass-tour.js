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
    tooltip.className = 'glass-tour-tooltip glass-tour-tooltip--entering';
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

  // ── Position the spotlight around a target element ──
  function positionSpotlight(el) {
    var rect = el.getBoundingClientRect();
    var pad = elPad(el);

    spotlight.style.top = (rect.top - pad) + 'px';
    spotlight.style.left = (rect.left - pad) + 'px';
    spotlight.style.width = (rect.width + pad * 2) + 'px';
    spotlight.style.height = (rect.height + pad * 2) + 'px';
    spotlight.style.borderRadius = pad > 0 ? '16px' : (getComputedStyle(el).borderRadius || '24px');
    spotlight.style.opacity = '1';
  }

  // ── Position tooltip: CENTERED on page, in the opposite half from the element ──
  function positionTooltip(el) {
    var rect = el.getBoundingClientRect();
    var pad = elPad(el);
    var vh = window.innerHeight;
    var vw = window.innerWidth;

    // Center horizontally
    var left = (vw - TOOLTIP_W) / 2;
    left = Math.max(VIEWPORT_PAD, Math.min(left, vw - TOOLTIP_W - VIEWPORT_PAD));

    // Determine which half the element center sits in
    var elCenterY = rect.top + rect.height / 2;
    var spotlightBottom = rect.bottom + pad;
    var spotlightTop = rect.top - pad;

    tooltip.style.left = left + 'px';
    tooltip.style.width = TOOLTIP_W + 'px';

    // Render offscreen first to measure height
    tooltip.style.top = '-9999px';
    tooltip.style.visibility = 'hidden';

    requestAnimationFrame(function () {
      var tooltipH = tooltip.offsetHeight;
      var top;

      if (elCenterY < vh / 2) {
        // Element is in the top half → put tooltip in the bottom half
        // Place tooltip so it doesn't overlap the spotlight
        var minTop = spotlightBottom + 20;
        // Center in the remaining bottom space
        var bottomSpace = vh - minTop;
        top = minTop + Math.max(0, (bottomSpace - tooltipH) / 2);
        // But never go off the bottom
        top = Math.min(top, vh - tooltipH - VIEWPORT_PAD);
      } else {
        // Element is in the bottom half → put tooltip in the top half
        var maxBottom = spotlightTop - 20;
        // Center in the remaining top space
        top = Math.max(VIEWPORT_PAD, (maxBottom - tooltipH) / 2);
        // But never overlap the spotlight
        if (top + tooltipH > spotlightTop - 20) {
          top = spotlightTop - 20 - tooltipH;
        }
        top = Math.max(VIEWPORT_PAD, top);
      }

      tooltip.style.top = top + 'px';
      tooltip.style.visibility = '';
    });
  }

  // ── Scroll: put element in one half, leave other half for tooltip ──
  function scrollForStep(el) {
    var rect = el.getBoundingClientRect();
    var pad = elPad(el);
    var vh = window.innerHeight;
    var elAbsTop = window.scrollY + rect.top;
    var elH = rect.height + pad * 2;

    // We want the element in the top ~45% of the viewport
    // so the bottom ~55% is free for the instruction panel
    // Target: element top at ~10% of viewport height
    var targetViewportTop = vh * 0.08;

    // If element is taller than 40% of viewport, just put it at the top
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

  // ── Render step content ──
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
    var nextLabel = isLast ? 'Got It!' : (isFirst && step.welcome) ? "Let's Go" : 'Next';

    var skipBtn = '<button class="glass-tour-btn glass-tour-btn--skip" data-tour-action="skip">' + skipLabel + '</button>';
    var nextBtn = '<button class="glass-tour-btn glass-tour-btn--next" data-tour-action="next">' + nextLabel + '</button>';

    return ''
      + '<div class="glass-tour-step-badge">Step ' + (index + 1) + ' of ' + total + '</div>'
      + '<div class="glass-tour-title">' + step.title + '</div>'
      + '<div class="glass-tour-body">' + step.body + '</div>'
      + '<div class="glass-tour-nav">'
      +   dotsHtml
      +   '<div class="glass-tour-btn-group">' + skipBtn + nextBtn + '</div>'
      + '</div>';
  }

  // ── Clear all inline styles from transitions ──
  function resetTooltipStyles() {
    tooltip.style.opacity = '';
    tooltip.style.transform = '';
    tooltip.style.transition = '';
    tooltip.style.top = '';
    tooltip.style.left = '';
    tooltip.style.width = '';
    tooltip.style.visibility = '';
  }

  // ── Render a targeted step (spotlight + tooltip) ──
  function renderTargetedStep(index, target, step) {
    tooltip.className = 'glass-tour-tooltip glass-tour-tooltip--entering';
    resetTooltipStyles();
    tooltip.innerHTML = renderStep(index);

    positionSpotlight(target);
    positionTooltip(target);

    bindButtons();

    // Animate in (after positionTooltip's RAF runs)
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          tooltip.classList.remove('glass-tour-tooltip--entering');
          tooltip.classList.add('glass-tour-tooltip--visible');
        });
      });
    });
  }

  // ── Show a step ──
  function showStep(index) {
    var step = steps[index];
    currentStep = index;

    resetTooltipStyles();

    if (step.welcome) {
      spotlight.style.opacity = '0';

      tooltip.className = 'glass-tour-welcome glass-tour-welcome--entering';
      tooltip.innerHTML = ''
        + '<div class="glass-tour-welcome-icon">' + (index === 0 ? '&#x1F44B;' : '&#x1F680;') + '</div>'
        + renderStep(index);

      bindButtons();

      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          tooltip.classList.remove('glass-tour-welcome--entering');
          tooltip.classList.add('glass-tour-welcome--visible');
        });
      });

    } else {
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

      // Fixed-position elements (like the Dispute Folder button) don't need scrolling
      if (step.noScroll) {
        requestAnimationFrame(function () {
          renderTargetedStep(index, target, step);
        });
        return;
      }

      // Scroll element into position
      scrollForStep(target);

      // Wait for scroll to settle, then render
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
      // Safety timeout if no scroll happens
      scrollTimer = setTimeout(doRender, 500);
    }
  }

  // ── Next step ──
  function next() {
    if (currentStep < steps.length - 1) {
      var nextIndex = currentStep + 1;

      tooltip.style.transition = 'opacity 0.25s ease, transform 0.25s ease';
      tooltip.style.opacity = '0';

      if (tooltip.classList.contains('glass-tour-welcome') || tooltip.classList.contains('glass-tour-welcome--visible')) {
        tooltip.style.transform = 'translate(-50%, -50%) scale(0.95)';
      } else {
        tooltip.style.transform = 'scale(0.97)';
      }

      setTimeout(function () {
        showStep(nextIndex);
      }, 280);
    } else {
      end();
    }
  }

  // ── End tour ──
  function end() {
    isActive = false;
    markCompleted();

    if (backdrop) {
      backdrop.style.opacity = '0';
      backdrop.style.transition = 'opacity 0.4s ease';
    }
    if (spotlight) {
      spotlight.style.opacity = '0';
    }
    if (tooltip) {
      tooltip.style.opacity = '0';
      tooltip.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
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
        positionTooltip(el);
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

    setTimeout(function () {
      showStep(0);
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
