const pptxgen = require("pptxgenjs");

let pres = new pptxgen();
pres.layout = "LAYOUT_16x9";
pres.author = "Bergomy Legendre";
pres.title = "uDispute — AI-Powered Credit Repair";

// ── Color palette: Midnight Glass ──
const C = {
  bg:        "0A0E1A",   // deep navy-black
  bgCard:    "141929",   // slightly lighter card
  accent:    "0077ED",   // uDispute blue
  accentLt:  "3D9BFF",   // lighter blue
  purple:    "6E5CE6",   // secondary accent
  white:     "FFFFFF",
  offWhite:  "E8ECF4",
  muted:     "8B95B0",   // tertiary text
  success:   "10B981",   // green
  warning:   "F59E0B",   // amber
  danger:    "EF4444",   // red
  glass:     "1E2640",   // glass card bg
};

// Helper: fresh shadow each call (pptxgenjs mutates objects)
const cardShadow = () => ({ type: "outer", blur: 12, offset: 3, angle: 135, color: "000000", opacity: 0.3 });
const softShadow = () => ({ type: "outer", blur: 8, offset: 2, angle: 135, color: "000000", opacity: 0.2 });

// ════════════════════════════════════════════
// SLIDE 1 — Title
// ════════════════════════════════════════════
let s1 = pres.addSlide();
s1.background = { color: C.bg };

// Decorative gradient circles (glass vibe)
s1.addShape(pres.shapes.OVAL, { x: -1.5, y: -1, w: 5, h: 5, fill: { color: C.accent, transparency: 85 } });
s1.addShape(pres.shapes.OVAL, { x: 7, y: 2.5, w: 4, h: 4, fill: { color: C.purple, transparency: 85 } });

s1.addText("uDispute", {
  x: 0.8, y: 1.2, w: 8.4, h: 1.2,
  fontSize: 54, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

s1.addText("AI-Powered Credit Repair", {
  x: 0.8, y: 2.3, w: 8.4, h: 0.7,
  fontSize: 28, fontFace: "Calibri Light",
  color: C.accentLt, align: "left", margin: 0
});

s1.addText("Take your credit into your own hands.", {
  x: 0.8, y: 3.3, w: 8.4, h: 0.6,
  fontSize: 18, fontFace: "Calibri",
  color: C.muted, italic: true, align: "left", margin: 0
});

s1.addShape(pres.shapes.RECTANGLE, {
  x: 0.8, y: 4.5, w: 1.6, h: 0.04, fill: { color: C.accent }
});

s1.addText("bergomy legendre  |  founder", {
  x: 0.8, y: 4.7, w: 5, h: 0.4,
  fontSize: 11, fontFace: "Calibri", color: C.muted, align: "left", margin: 0, charSpacing: 2
});


// ════════════════════════════════════════════
// SLIDE 2 — The Problem
// ════════════════════════════════════════════
let s2 = pres.addSlide();
s2.background = { color: C.bg };

s2.addText("The Problem", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

s2.addText("Credit repair is a broken industry.", {
  x: 0.8, y: 1.2, w: 9, h: 0.5,
  fontSize: 16, fontFace: "Calibri", color: C.muted, align: "left", margin: 0
});

// Problem cards
const problems = [
  { stat: "1 in 5", label: "Americans have errors\non their credit report", sub: "— FTC Study" },
  { stat: "$5B+", label: "Industry dominated by\noutdated template tools", sub: "Copy-paste to Word" },
  { stat: "0%", label: "Transparency in pricing,\nprocess, or outcomes", sub: "Opaque by design" },
];

problems.forEach((p, i) => {
  let x = 0.8 + i * 3.05;
  s2.addShape(pres.shapes.RECTANGLE, {
    x: x, y: 2.1, w: 2.75, h: 2.8,
    fill: { color: C.glass }, shadow: cardShadow()
  });
  s2.addText(p.stat, {
    x: x, y: 2.3, w: 2.75, h: 0.9,
    fontSize: 36, fontFace: "Georgia", bold: true,
    color: C.danger, align: "center", margin: 0
  });
  s2.addText(p.label, {
    x: x + 0.2, y: 3.2, w: 2.35, h: 0.9,
    fontSize: 13, fontFace: "Calibri", color: C.offWhite, align: "center", margin: 0
  });
  s2.addText(p.sub, {
    x: x + 0.2, y: 4.1, w: 2.35, h: 0.4,
    fontSize: 10, fontFace: "Calibri", color: C.muted, italic: true, align: "center", margin: 0
  });
});


// ════════════════════════════════════════════
// SLIDE 3 — The Solution
// ════════════════════════════════════════════
let s3 = pres.addSlide();
s3.background = { color: C.bg };

s3.addShape(pres.shapes.OVAL, { x: 6.5, y: -1, w: 5, h: 5, fill: { color: C.accent, transparency: 88 } });

s3.addText("The Solution", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

s3.addText([
  { text: "u", options: { color: C.accent, bold: true, fontSize: 22 } },
  { text: "Dispute replaces the entire broken workflow.", options: { color: C.offWhite, fontSize: 22 } },
], { x: 0.8, y: 1.3, w: 8, h: 0.6, fontFace: "Calibri", margin: 0 });

s3.addText([
  { text: "AI generates dispute letters using real legal strategy — FCRA, FDCPA, ACDV enforcement.", options: { breakLine: true, color: C.offWhite } },
  { text: "", options: { breakLine: true, fontSize: 8 } },
  { text: "Direct bureau mailing built in. No copy-paste. No Word docs.", options: { breakLine: true, color: C.offWhite } },
  { text: "", options: { breakLine: true, fontSize: 8 } },
  { text: "Round-by-round escalation. The AI adjusts strategy when bureaus push back.", options: { breakLine: true, color: C.offWhite } },
  { text: "", options: { breakLine: true, fontSize: 8 } },
  { text: "Transparent by design. You see everything — every letter, every round, every outcome.", options: { color: C.offWhite } },
], {
  x: 0.8, y: 2.2, w: 7, h: 2.8,
  fontSize: 15, fontFace: "Calibri", margin: 0
});

s3.addText([
  { text: "The ", options: { color: C.muted } },
  { text: '"u"', options: { color: C.accent, bold: true } },
  { text: " in uDispute means ", options: { color: C.muted } },
  { text: "you.", options: { color: C.white, bold: true } },
  { text: " You should be in control.", options: { color: C.muted } },
], {
  x: 0.8, y: 4.7, w: 8, h: 0.5,
  fontSize: 13, fontFace: "Calibri", italic: true, margin: 0
});


// ════════════════════════════════════════════
// SLIDE 4 — How It Works
// ════════════════════════════════════════════
let s4 = pres.addSlide();
s4.background = { color: C.bg };

s4.addText("How It Works", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

s4.addText("Four steps. That's it.", {
  x: 0.8, y: 1.1, w: 9, h: 0.4,
  fontSize: 15, fontFace: "Calibri", color: C.muted, align: "left", margin: 0
});

const steps = [
  { num: "01", title: "Upload", desc: "Upload your credit\nreport PDF" },
  { num: "02", title: "Analyze", desc: "AI reads every account,\nscores, and negatives" },
  { num: "03", title: "Generate", desc: "AI drafts dispute letters\nusing legal strategy" },
  { num: "04", title: "Mail", desc: "One click sends directly\nto all three bureaus" },
];

steps.forEach((step, i) => {
  let x = 0.5 + i * 2.35;
  // Card
  s4.addShape(pres.shapes.RECTANGLE, {
    x: x, y: 1.8, w: 2.15, h: 3.0,
    fill: { color: C.glass }, shadow: cardShadow()
  });
  // Number
  s4.addText(step.num, {
    x: x, y: 1.95, w: 2.15, h: 0.8,
    fontSize: 32, fontFace: "Georgia", bold: true,
    color: C.accent, align: "center", margin: 0
  });
  // Title
  s4.addText(step.title, {
    x: x, y: 2.7, w: 2.15, h: 0.5,
    fontSize: 18, fontFace: "Calibri", bold: true,
    color: C.white, align: "center", margin: 0
  });
  // Description
  s4.addText(step.desc, {
    x: x + 0.15, y: 3.3, w: 1.85, h: 1.0,
    fontSize: 12, fontFace: "Calibri",
    color: C.muted, align: "center", margin: 0
  });

  // Arrow between cards (not after last)
  if (i < 3) {
    s4.addText("\u2192", {
      x: x + 2.15, y: 2.8, w: 0.2, h: 0.5,
      fontSize: 18, color: C.accent, align: "center", margin: 0
    });
  }
});


// ════════════════════════════════════════════
// SLIDE 5 — Product Features
// ════════════════════════════════════════════
let s5 = pres.addSlide();
s5.background = { color: C.bg };

s5.addText("Product Features", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

const features = [
  { icon: "\uD83E\uDD16", title: "AI Dispute Engine", desc: "GPT-powered letter generation with legal strategy — not generic templates." },
  { icon: "\uD83D\uDCEC", title: "Direct Bureau Mailing", desc: "Mail letters to Equifax, TransUnion, and Experian via DocuPost USPS." },
  { icon: "\u2694\uFE0F", title: "Prompt Packs", desc: "Default, Consumer Law, Arbitration, ACDV Enforcement — switch strategies per round." },
  { icon: "\uD83D\uDCC8", title: "Auto Escalation", desc: "AI adjusts strategy round-by-round based on bureau responses." },
  { icon: "\uD83D\uDCC1", title: "Dispute Folder", desc: "Every letter, every round, fully tracked and searchable." },
  { icon: "\uD83D\uDD04", title: "PDF Merge", desc: "Supporting docs merged into dispute packages automatically." },
];

features.forEach((f, i) => {
  let col = i % 2;
  let row = Math.floor(i / 2);
  let x = 0.8 + col * 4.5;
  let y = 1.4 + row * 1.3;

  s5.addShape(pres.shapes.RECTANGLE, {
    x: x, y: y, w: 4.2, h: 1.1,
    fill: { color: C.glass }, shadow: softShadow()
  });
  s5.addText(f.icon, {
    x: x + 0.15, y: y + 0.1, w: 0.6, h: 0.9,
    fontSize: 24, align: "center", valign: "middle", margin: 0
  });
  s5.addText(f.title, {
    x: x + 0.8, y: y + 0.1, w: 3.2, h: 0.4,
    fontSize: 14, fontFace: "Calibri", bold: true,
    color: C.white, align: "left", valign: "middle", margin: 0
  });
  s5.addText(f.desc, {
    x: x + 0.8, y: y + 0.5, w: 3.2, h: 0.5,
    fontSize: 11, fontFace: "Calibri",
    color: C.muted, align: "left", valign: "top", margin: 0
  });
});


// ════════════════════════════════════════════
// SLIDE 6 — The Technology
// ════════════════════════════════════════════
let s6 = pres.addSlide();
s6.background = { color: C.bg };

s6.addShape(pres.shapes.OVAL, { x: -2, y: 3, w: 5, h: 5, fill: { color: C.purple, transparency: 90 } });

s6.addText("The Technology", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

// Left column — AI Engine
s6.addShape(pres.shapes.RECTANGLE, {
  x: 0.8, y: 1.5, w: 4, h: 3.5,
  fill: { color: C.glass }, shadow: cardShadow()
});
s6.addText("AI Engine", {
  x: 0.8, y: 1.7, w: 4, h: 0.5,
  fontSize: 20, fontFace: "Calibri", bold: true,
  color: C.accent, align: "center", margin: 0
});
s6.addText([
  { text: "OpenAI GPT generates dispute letters contextually — not from static templates.", options: { breakLine: true } },
  { text: "", options: { breakLine: true, fontSize: 6 } },
  { text: "Each letter references the specific account, bureau, and legal violation.", options: { breakLine: true } },
  { text: "", options: { breakLine: true, fontSize: 6 } },
  { text: "The system learns which strategies work and escalates across rounds.", options: {} },
], {
  x: 1.1, y: 2.3, w: 3.4, h: 2.4,
  fontSize: 12, fontFace: "Calibri", color: C.offWhite, margin: 0
});

// Right column — Legal Strategy
s6.addShape(pres.shapes.RECTANGLE, {
  x: 5.2, y: 1.5, w: 4, h: 3.5,
  fill: { color: C.glass }, shadow: cardShadow()
});
s6.addText("Legal Strategy Packs", {
  x: 5.2, y: 1.7, w: 4, h: 0.5,
  fontSize: 20, fontFace: "Calibri", bold: true,
  color: C.purple, align: "center", margin: 0
});
s6.addText([
  { text: "Default", options: { bold: true, color: C.white, breakLine: true } },
  { text: "Clean first-round dispute templates.", options: { breakLine: true, color: C.muted } },
  { text: "", options: { breakLine: true, fontSize: 6 } },
  { text: "Consumer Law", options: { bold: true, color: C.white, breakLine: true } },
  { text: "Cites FCBA, FDCPA, and FCRA statutes.", options: { breakLine: true, color: C.muted } },
  { text: "", options: { breakLine: true, fontSize: 6 } },
  { text: "Arbitration", options: { bold: true, color: C.white, breakLine: true } },
  { text: "Heavy hitter — 15 U.S.C. \u00A71681e(b).", options: { breakLine: true, color: C.muted } },
  { text: "", options: { breakLine: true, fontSize: 6 } },
  { text: "ACDV Enforcement", options: { bold: true, color: C.white, breakLine: true } },
  { text: "Demands full verification records.", options: { color: C.muted } },
], {
  x: 5.5, y: 2.3, w: 3.4, h: 2.4,
  fontSize: 12, fontFace: "Calibri", margin: 0
});


// ════════════════════════════════════════════
// SLIDE 7 — Business Model / Pricing
// ════════════════════════════════════════════
let s7 = pres.addSlide();
s7.background = { color: C.bg };

s7.addText("Business Model", {
  x: 0.8, y: 0.3, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

const plans = [
  {
    name: "Free", price: "$0", color: C.muted,
    features: ["Upload credit report", "AI analysis", "Generate dispute letters", "3 manual accounts", "48hr cooldown"]
  },
  {
    name: "Pro", price: "$29/mo", color: C.accent,
    features: ["Unlimited disputes", "Prompt pack switching", "Direct bureau mailing", "Dispute folder & tracking", "Escalation engine"]
  },
  {
    name: "Business", price: "$79/mo", color: C.purple,
    features: ["Full CRM & pipelines", "Autonomous AI agent", "Client portal", "CFPB search", "Supervised + auto modes"]
  },
];

plans.forEach((plan, i) => {
  let x = 0.5 + i * 3.15;
  let isPro = i === 1;

  s7.addShape(pres.shapes.RECTANGLE, {
    x: x, y: 1.2, w: 2.95, h: 3.9,
    fill: { color: isPro ? C.glass : C.bgCard },
    shadow: isPro ? cardShadow() : softShadow(),
    line: isPro ? { color: C.accent, width: 1.5 } : undefined
  });

  s7.addText(plan.name, {
    x: x, y: 1.35, w: 2.95, h: 0.5,
    fontSize: 18, fontFace: "Calibri", bold: true,
    color: plan.color, align: "center", margin: 0
  });
  s7.addText(plan.price, {
    x: x, y: 1.85, w: 2.95, h: 0.5,
    fontSize: 28, fontFace: "Georgia", bold: true,
    color: C.white, align: "center", margin: 0
  });

  let bulletItems = plan.features.map((f, fi) => ({
    text: f,
    options: {
      bullet: true, breakLine: fi < plan.features.length - 1,
      color: C.offWhite, fontSize: 11
    }
  }));

  s7.addText(bulletItems, {
    x: x + 0.25, y: 2.6, w: 2.5, h: 2.2,
    fontFace: "Calibri", margin: 0, paraSpaceAfter: 4
  });
});


// ════════════════════════════════════════════
// SLIDE 8 — Market Opportunity
// ════════════════════════════════════════════
let s8 = pres.addSlide();
s8.background = { color: C.bg };

s8.addShape(pres.shapes.OVAL, { x: 7, y: -1.5, w: 5, h: 5, fill: { color: C.accent, transparency: 90 } });

s8.addText("Market Opportunity", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

// Big stats
const stats = [
  { num: "$5B+", label: "Credit repair\nindustry size" },
  { num: "79M", label: "Americans with\ncollections on file" },
  { num: "20%", label: "Have at least one\nerror on their report" },
];

stats.forEach((st, i) => {
  let x = 0.8 + i * 3.05;
  s8.addShape(pres.shapes.RECTANGLE, {
    x: x, y: 1.5, w: 2.75, h: 2.0,
    fill: { color: C.glass }, shadow: cardShadow()
  });
  s8.addText(st.num, {
    x: x, y: 1.6, w: 2.75, h: 0.8,
    fontSize: 32, fontFace: "Georgia", bold: true,
    color: C.accent, align: "center", margin: 0
  });
  s8.addText(st.label, {
    x: x + 0.2, y: 2.4, w: 2.35, h: 0.8,
    fontSize: 12, fontFace: "Calibri",
    color: C.offWhite, align: "center", margin: 0
  });
});

s8.addText([
  { text: "The tools available today are outdated, template-based, and opaque.", options: { breakLine: true } },
  { text: "Credit repair businesses charge $500\u2013$5,000 per client using the same Word docs from 2015.", options: { breakLine: true } },
  { text: "There is no modern, AI-native platform serving this market.", options: { bold: true } },
], {
  x: 0.8, y: 3.8, w: 8.5, h: 1.5,
  fontSize: 13, fontFace: "Calibri", color: C.muted, margin: 0, paraSpaceAfter: 6
});


// ════════════════════════════════════════════
// SLIDE 9 — Competitive Landscape
// ════════════════════════════════════════════
let s9 = pres.addSlide();
s9.background = { color: C.bg };

s9.addText("Why uDispute Wins", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

// Comparison table
const headers = [
  [
    { text: "", options: { fill: { color: C.bgCard }, color: C.white, bold: true, fontSize: 12 } },
    { text: "Traditional Tools", options: { fill: { color: C.bgCard }, color: C.muted, bold: true, fontSize: 12, align: "center" } },
    { text: "uDispute", options: { fill: { color: C.accent }, color: C.white, bold: true, fontSize: 12, align: "center" } },
  ]
];

const rows = [
  ["Letter Generation", "Static templates", "AI + legal strategy"],
  ["Bureau Mailing", "Manual / copy-paste", "Built-in DocuPost"],
  ["Escalation", "None", "Auto round-by-round"],
  ["Tracking", "Spreadsheets", "Full dispute folder"],
  ["Business Tools", "Basic CRM", "CRM + AI Agent + Portal"],
  ["Transparency", "Opaque pricing", "Glass by design"],
];

const tableRows = headers.concat(rows.map(r => [
  { text: r[0], options: { fill: { color: C.glass }, color: C.offWhite, fontSize: 11, bold: true } },
  { text: r[1], options: { fill: { color: C.glass }, color: C.muted, fontSize: 11, align: "center" } },
  { text: r[2], options: { fill: { color: C.glass }, color: C.success, fontSize: 11, align: "center", bold: true } },
]));

s9.addTable(tableRows, {
  x: 0.8, y: 1.4, w: 8.4,
  colW: [2.8, 2.8, 2.8],
  border: { pt: 0.5, color: "2A3050" },
  rowH: 0.5,
  margin: [0.1, 0.2, 0.1, 0.2],
});


// ════════════════════════════════════════════
// SLIDE 10 — Traction / Beta
// ════════════════════════════════════════════
let s10 = pres.addSlide();
s10.background = { color: C.bg };

s10.addShape(pres.shapes.OVAL, { x: -1, y: 2, w: 4, h: 4, fill: { color: C.purple, transparency: 88 } });

s10.addText("Traction", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

s10.addText("Where we are today.", {
  x: 0.8, y: 1.1, w: 9, h: 0.4,
  fontSize: 15, fontFace: "Calibri", color: C.muted, align: "left", margin: 0
});

const milestones = [
  { icon: "\u2705", text: "Full product built — Free, Pro, and Business plans functional" },
  { icon: "\u2705", text: "AI dispute engine live with 4 legal strategy packs" },
  { icon: "\u2705", text: "DocuPost integration — direct USPS mailing operational" },
  { icon: "\u2705", text: "Business CRM with autonomous AI agent pipeline" },
  { icon: "\uD83D\uDE80", text: "Closed beta launching — invite-only through Skool community" },
  { icon: "\uD83C\uDFAF", text: "Targeting credit repair professionals + individuals" },
];

milestones.forEach((m, i) => {
  s10.addText(m.icon + "  " + m.text, {
    x: 1.2, y: 1.8 + i * 0.55, w: 7.5, h: 0.45,
    fontSize: 14, fontFace: "Calibri", color: C.offWhite, align: "left", valign: "middle", margin: 0
  });
});


// ════════════════════════════════════════════
// SLIDE 11 — Vision / Roadmap
// ════════════════════════════════════════════
let s11 = pres.addSlide();
s11.background = { color: C.bg };

s11.addText("Roadmap", {
  x: 0.8, y: 0.4, w: 9, h: 0.8,
  fontSize: 36, fontFace: "Georgia", bold: true,
  color: C.white, align: "left", margin: 0
});

const roadmap = [
  { phase: "NOW", title: "Closed Beta", items: "Invite-only beta with Skool community\nCollect feedback, iterate on UX\nValidate mailing pipeline at scale", color: C.accent },
  { phase: "Q2 2026", title: "Public Launch", items: "Open signups\nStripe billing integration\nMobile-responsive polish", color: C.accentLt },
  { phase: "Q3 2026", title: "Scale", items: "API for credit repair businesses\nWhite-label option\nAdvanced analytics dashboard", color: C.purple },
];

roadmap.forEach((r, i) => {
  let x = 0.5 + i * 3.15;
  s11.addShape(pres.shapes.RECTANGLE, {
    x: x, y: 1.5, w: 2.95, h: 3.5,
    fill: { color: C.glass }, shadow: cardShadow()
  });
  s11.addText(r.phase, {
    x: x, y: 1.65, w: 2.95, h: 0.4,
    fontSize: 11, fontFace: "Calibri", bold: true,
    color: r.color, align: "center", charSpacing: 3, margin: 0
  });
  s11.addText(r.title, {
    x: x, y: 2.05, w: 2.95, h: 0.5,
    fontSize: 18, fontFace: "Calibri", bold: true,
    color: C.white, align: "center", margin: 0
  });
  s11.addText(r.items, {
    x: x + 0.25, y: 2.7, w: 2.45, h: 2.0,
    fontSize: 11, fontFace: "Calibri",
    color: C.muted, align: "left", margin: 0
  });
});


// ════════════════════════════════════════════
// SLIDE 12 — Call to Action
// ════════════════════════════════════════════
let s12 = pres.addSlide();
s12.background = { color: C.bg };

s12.addShape(pres.shapes.OVAL, { x: -1.5, y: -1, w: 5, h: 5, fill: { color: C.accent, transparency: 85 } });
s12.addShape(pres.shapes.OVAL, { x: 7, y: 2.5, w: 4, h: 4, fill: { color: C.purple, transparency: 85 } });

s12.addText([
  { text: "u", options: { color: C.accent, fontSize: 48, bold: true } },
  { text: " have the power.", options: { color: C.white, fontSize: 48 } },
], {
  x: 0.8, y: 1.0, w: 8.4, h: 1.2,
  fontFace: "Georgia", align: "center", margin: 0
});

s12.addText("Start now.", {
  x: 0.8, y: 2.2, w: 8.4, h: 0.6,
  fontSize: 22, fontFace: "Calibri Light",
  color: C.accentLt, align: "center", margin: 0
});

s12.addShape(pres.shapes.RECTANGLE, {
  x: 3.5, y: 3.2, w: 3, h: 0.04, fill: { color: C.accent, transparency: 50 }
});

s12.addText("Request beta access or learn more:", {
  x: 0.8, y: 3.6, w: 8.4, h: 0.4,
  fontSize: 14, fontFace: "Calibri", color: C.muted, align: "center", margin: 0
});

s12.addText("beta.udispute.com", {
  x: 0.8, y: 4.1, w: 8.4, h: 0.5,
  fontSize: 20, fontFace: "Calibri", bold: true,
  color: C.accent, align: "center", margin: 0
});

s12.addText("bergomy legendre  |  founder  |  bergomylegendre@gmail.com", {
  x: 0.8, y: 4.8, w: 8.4, h: 0.4,
  fontSize: 11, fontFace: "Calibri", color: C.muted, align: "center", margin: 0, charSpacing: 1
});


// ── Write file ──
pres.writeFile({ fileName: "/Users/bergomylegendre/Desktop/training.py/uDispute_PitchDeck.pptx" })
  .then(() => console.log("Pitch deck saved!"))
  .catch(err => console.error("Error:", err));
