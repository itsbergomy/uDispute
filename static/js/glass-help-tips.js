/* ═══════════════════════════════════════════════════════════
   Glass Help Tips — Contextual jargon tooltips
   Part of the Liquid Glass Design System v2

   Usage:
     <span class="glass-help-tip" data-term="bureau">?</span>

   Auto-initializes on DOMContentLoaded.
   ═══════════════════════════════════════════════════════════ */

window.GlassHelpTips = (function () {
  'use strict';

  var DEFINITIONS = {
    'bureau': 'A credit bureau (Equifax, TransUnion, Experian) is a company that collects your credit history and sells it to lenders as a credit report. Errors on these reports can hurt your score.',
    'prompt-pack': 'A prompt pack is a pre-built legal strategy that controls how your dispute letter is worded. Different packs cite different federal laws for stronger arguments.',
    'acdv': 'ACDV (Automated Consumer Dispute Verification) is the electronic system bureaus use to verify disputed items with creditors. An ACDV enforcement letter demands they prove they actually ran this process.',
    'round': 'A dispute round is one cycle of sending letters and waiting for responses. Most credit repair involves multiple rounds, escalating the legal strategy each time.',
    'dispute-mode': 'Dispute Mode uses AI to read your credit report PDF, extract negative accounts, and generate personalized dispute letters automatically.',
    'manual-mode': 'Manual Mode lets you type in account details by hand instead of uploading a PDF. You still get an AI-generated dispute letter.',
    'escalation': 'Escalation means increasing legal pressure in later dispute rounds \u2014 moving from simple disputes to citing specific federal statutes like FCRA and FDCPA.',
    'supporting-docs': 'Supporting documents are evidence you attach to strengthen your dispute \u2014 bank statements, payment receipts, or prior correspondence with bureaus.',
    'fcra': 'The Fair Credit Reporting Act (FCRA) is the federal law that gives you the right to dispute inaccurate items on your credit report and requires bureaus to investigate within 30 days.',
    'certified-mail': 'Certified mail provides proof that your letter was delivered. It creates a legal paper trail showing the bureau received your dispute.',
    'docupost': 'DocuPost is the mailing service uDispute uses to send your dispute letters via USPS. Your letters are printed, stuffed, and mailed automatically.',
    'dispute-folder': 'Your Dispute Folder stores every letter, log entry, and document you\u2019ve created \u2014 organized and searchable. It\u2019s your personal case file.'
  };

  var activePopup = null;
  var activeTimer = null;

  function createPopup(term, anchorEl) {
    dismiss();

    var def = DEFINITIONS[term];
    if (!def) return;

    var popup = document.createElement('div');
    popup.className = 'glass-help-popup';
    popup.innerHTML = '<div class="glass-help-popup-term">' + formatTerm(term) + '</div>' + def;
    document.body.appendChild(popup);

    // Position near the ? icon
    var rect = anchorEl.getBoundingClientRect();
    var popupW = 280;
    var vh = window.innerHeight;
    var vw = window.innerWidth;

    var left = rect.left + rect.width / 2 - popupW / 2;
    left = Math.max(12, Math.min(left, vw - popupW - 12));

    // Prefer below, fall back to above
    var top = rect.bottom + 8;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
    popup.style.width = popupW + 'px';

    // After render, check if it overflows bottom
    requestAnimationFrame(function () {
      var popH = popup.offsetHeight;
      if (top + popH > vh - 12) {
        popup.style.top = (rect.top - popH - 8) + 'px';
      }
    });

    activePopup = popup;

    // Auto-dismiss after 6 seconds
    activeTimer = setTimeout(dismiss, 6000);
  }

  function formatTerm(term) {
    return term.replace(/-/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  function dismiss() {
    if (activePopup) {
      activePopup.remove();
      activePopup = null;
    }
    if (activeTimer) {
      clearTimeout(activeTimer);
      activeTimer = null;
    }
  }

  function init() {
    var tips = document.querySelectorAll('.glass-help-tip');
    for (var i = 0; i < tips.length; i++) {
      (function (el) {
        var term = el.getAttribute('data-term');
        if (!term || !DEFINITIONS[term]) return;

        el.addEventListener('mouseenter', function () {
          createPopup(term, el);
        });

        el.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          if (activePopup) {
            dismiss();
          } else {
            createPopup(term, el);
          }
        });

        el.addEventListener('mouseleave', function () {
          // Small delay so user can move mouse to popup
          setTimeout(function () {
            if (activePopup && !activePopup.matches(':hover')) {
              dismiss();
            }
          }, 300);
        });
      })(tips[i]);
    }

    // Dismiss on outside click
    document.addEventListener('click', function (e) {
      if (activePopup && !e.target.closest('.glass-help-tip') && !e.target.closest('.glass-help-popup')) {
        dismiss();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { init: init, dismiss: dismiss };
})();
