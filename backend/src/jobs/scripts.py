"""Scripts Orbit runs on job sites' pages, in the browser in use: never written by the
model. Each returns JSON."""

import json

# The postings a results page lists: each one's address, and the lines of text on its
# card. `{posting}` is the pattern a posting's address matches. The page is scrolled to
# its end, and so is its list, the part of it that scrolls and holds the most
# postings' links, so that a lazy list loads every card. A list may show only the
# cards in view, as LinkedIn's does, so cards are read at each step of the way: a
# posting's card in the list is kept over one elsewhere, as beside it.
LISTINGS = r"""async () => {
  const posting = new RegExp(%(posting)s);
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  // Where a link leads: a search engine's link that passes through it first is read
  // for the address it passes on.
  const target = (a) => {
    try {
      const url = new URL(a.href);
      if (/(^|\.)google\.|(^|\.)duckduckgo\.|(^|\.)bing\./.test(url.hostname)) {
        for (const key of ['q', 'url', 'uddg']) {
          const value = url.searchParams.get(key);
          if (value && /^https?:/.test(value)) return value;
        }
      }
    } catch (e) {}
    return a.href;
  };
  const links = () => Array.from(document.querySelectorAll('a[href]')).map((a) => [a, target(a)]);
  const scrolls = (el) => el.scrollHeight > el.clientHeight + 8 &&
    /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);
  const counts = new Map();
  for (const [a, href] of links()) {
    if (!posting.test(href)) continue;
    let el = a.parentElement;
    while (el && el !== document.body && el !== document.documentElement && !scrolls(el)) el = el.parentElement;
    if (el && el !== document.body && el !== document.documentElement) counts.set(el, (counts.get(el) || 0) + 1);
  }
  let list = null;
  for (const [el, count] of counts) if (!list || count > counts.get(list)) list = el;
  // A posting's title is the text of a link to it that is no button, a board may
  // link a posting's Apply button to it too, and best a link's heading, as a
  // search result's is.
  const button = /^(?:apply(?: now)?|view(?: job)?|learn more|details|see more|more)$/i;
  const titles = new Map();
  // A posting's card is what holds its link and no other posting's. It ends where a
  // list holds each of its items, and never takes in the page around it: its menus
  // and top bar, its main part, or more text than a card has.
  const chrome = 'header, nav, footer, [role="banner"], [role="navigation"], ' +
    '[role="contentinfo"], [aria-live]';
  const found = new Map();
  const read = () => {
    const all = links();
    for (const [a, href] of all) {
      const match = href.match(posting);
      if (!match) continue;
      const heading = a.querySelector('h1, h2, h3, h4, [role="heading"]');
      const text = ((heading || a).innerText || '').trim().split('\n')[0].trim();
      const known = titles.get(match[1]);
      if (text && !button.test(text) && (!known || (heading && !known.heading))) {
        titles.set(match[1], {text, heading: !!heading});
      }
    }
    const seen = new Set();
    for (const [a, href] of all) {
      const match = href.match(posting);
      if (!match || seen.has(match[1])) continue;
      seen.add(match[1]);
      const listed = !!list && list.contains(a);
      const before = found.get(match[1]);
      if (before && (before.listed || !listed)) continue;
      let card = a;
      while (card.parentElement && card.parentElement !== document.body) {
        if (/^(?:LI|ARTICLE|TR)$/.test(card.tagName)) break;
        const up = card.parentElement;
        if (up.matches('main, [role="main"]') || up === list) break;
        if (Array.from(up.querySelectorAll(chrome)).some((part) => !card.contains(part))) break;
        if ((up.innerText || '').length > 1500) break;
        const holds = all.some(([other, to]) => {
          if (!up.contains(other)) return false;
          const m = to.match(posting);
          return m && m[1] !== match[1];
        });
        if (holds) break;
        card = up;
      }
      const lines = [];
      for (const line of (card.innerText || '').split('\n')) {
        const text = line.trim().replace(/\s+/g, ' ');
        if (text && !lines.includes(text)) lines.push(text.slice(0, 160));
      }
      if (lines.length) found.set(match[1], {id: match[1], href, lines: lines.slice(0, 12), listed});
    }
  };
  read();
  const steps = list ? Math.min(30, Math.max(12, Math.ceil(list.scrollHeight / Math.max(1, list.clientHeight * 0.8)))) : 12;
  for (let step = 1; step <= steps; step++) {
    if (list) list.scrollTop = list.scrollHeight * step / steps;
    window.scrollTo(0, document.body.scrollHeight * step / steps);
    await wait(250);
    read();
  }
  return Array.from(found.values()).map(({id, href, lines}) => {
    const title = titles.get(id);
    return {id, href, title: title ? title.text : '', lines};
  });
}"""

# A posting's page, once it has stopped changing: scrolled down, a step at a time, as
# a site may fill in what is below only once it is in view, until the text the page
# shows has stayed the same for a while, or for at most some seconds. Returns how
# long the text is.
SETTLED = r"""async () => {
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const size = () => (document.body ? (document.body.innerText || '').length : 0);
  const scrolls = (el) => el.clientHeight > 200 && el.scrollHeight > el.clientHeight + 8 &&
    /(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY);
  const parts = () => Array.from(document.querySelectorAll('main, [role="main"], section, aside, div')).filter(scrolls);
  let boxes = parts();
  let last = -1;
  let still = 0;
  for (let step = 1; step <= 24 && (step <= 4 || still < 2); step++) {
    const down = Math.min(step, 4) / 4;
    window.scrollTo(0, (document.body ? document.body.scrollHeight : 0) * down);
    for (const box of boxes) box.scrollTop = box.scrollHeight * down;
    await wait(250);
    const now = size();
    still = now === last && now > 0 ? still + 1 : 0;
    last = now;
    if (step % 4 === 0) boxes = parts();
  }
  window.scrollTo(0, 0);
  return last;
}"""

# What a posting's page says of the job in the shape search engines read: the first
# JobPosting in its JSON-LD, without its description, or null when it has none.
JOB_POSTING = r"""() => {
  const found = [];
  const visit = (node) => {
    if (Array.isArray(node)) return node.forEach(visit);
    if (!node || typeof node !== 'object') return;
    const types = [].concat(node['@type'] || []).map(String);
    if (types.some((type) => /(?:^|[/:])JobPosting$/i.test(type))) found.push(node);
    if (node['@graph']) visit(node['@graph']);
  };
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { visit(JSON.parse(script.textContent)); } catch (e) {}
  }
  if (!found.length) return null;
  const kept = {};
  for (const key of ['jobLocation', 'jobLocationType', 'applicantLocationRequirements',
      'datePosted', 'experienceRequirements', 'baseSalary']) {
    if (found[0][key] !== undefined) kept[key] = found[0][key];
  }
  return kept;
}"""

# The buttons and links a posting page offers for applying: what each says, and where
# a link goes.
APPLYING = r"""() => {
  const found = [];
  for (const el of document.querySelectorAll('a, button, [role="button"]')) {
    const said = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().replace(/\s+/g, ' ');
    if (!/apply/i.test(said) || said.length > 120) continue;
    found.push({said, href: el.tagName === 'A' ? el.href : ''});
  }
  return found.slice(0, 20);
}"""


# How many postings a company's job board lists, once its page has shown them: zero
# when it is no board at all.
BOARD_POSTINGS = r"""async () => {
  const posting = new RegExp(%(posting)s);
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  for (let tries = 0; tries < 10; tries++) {
    const count = Array.from(document.querySelectorAll('a[href]')).filter((a) => posting.test(a.href)).length;
    if (count) return count;
    await wait(300);
  }
  return 0;
}"""


def board_postings(posting_pattern: str) -> str:
    """The script that counts the postings on a company's job board."""
    return BOARD_POSTINGS % {"posting": json.dumps(posting_pattern)}


def listings(posting_pattern: str) -> str:
    """The listings script, for postings whose address matches the pattern."""
    return LISTINGS % {"posting": json.dumps(posting_pattern)}


# An application form as the page holds it: each field with its question, kind,
# whether it is required, its length limit, its options and its value, and a selector
# that finds it again; the button that submits the form, one that goes on to a next
# step, frames holding an application from elsewhere, whether the page asks for a
# password or shows a check that the user is a person, and the errors it shows. A
# drop-down that lists its options only once opened is left for the browser to open.
FORM = r"""() => {
  const css = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^\w-]/g, '\\$&');
  const quote = (s) => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const unique = (selector) => { try { return document.querySelectorAll(selector).length === 1; } catch (e) { return false; } };
  const selectorOf = (el) => {
    if (el.id && unique('#' + css(el.id))) return '#' + css(el.id);
    if (el.name) {
      const byName = el.tagName.toLowerCase() + '[name="' + quote(el.name) + '"]';
      if (unique(byName)) return byName;
      const withValue = byName + '[value="' + quote(el.value) + '"]';
      if ((el.type === 'radio' || el.type === 'checkbox') && unique(withValue)) return withValue;
    }
    const path = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      if (node.id && unique('#' + css(node.id))) { path.unshift('#' + css(node.id)); break; }
      let index = 1;
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (sibling.tagName === node.tagName) index++;
      }
      path.unshift(node.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
      node = node.parentElement;
    }
    return path.join(' > ');
  };
  const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  const MARK = /[*✱]\s*$|✱|\(required\)/i;
  const marker = (text) => text.replace(/[*✱\s]/g, '').length < 2;
  // What a button that opens a file chooser says, and what a placeholder or a hint
  // says that is no question.
  const CHOOSING = /^(attach|upload|choose|browse|select|drop|or|enter manually|dropbox|google drive)\b/i;
  const HINT = /^(type|start typing|enter|pick|select|choose|search|your answer|write|e\.g\.|ex:|optional)\b|\.\.\.$|…$/i;
  const SKIPPED = ['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password'];
  const counted = (f) => {
    const type = (f.getAttribute('type') || f.tagName).toLowerCase();
    if (SKIPPED.includes(type) || f.disabled || f.getAttribute('aria-hidden') === 'true') return false;
    return type === 'file' || shown(f);
  };
  const BLOCK = 'p, div, label, legend, h1, h2, h3, h4, h5, h6, li, td, th, dt, dd, fieldset, section, span[class*="label" i], span[class*="title" i]';
  // Not words of a question: a list's options, buttons, and what a drop-down shows
  // in itself, its placeholder or the option chosen.
  const VALUE = '[class*="single-value" i], [class*="singleValue" i], [class*="multi-value" i], [class*="multiValue" i]';
  const LEFT_OUT = 'select, textarea, option, button, [role="button"], [role="listbox"], [role="option"], script, style, noscript, [class*="placeholder" i], ' + VALUE;
  // The words shown in part of the page, in blocks, without what `skip` matches.
  const blocksIn = (root, skip, before) => {
    const blocks = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (before && !(node.compareDocumentPosition(before) & Node.DOCUMENT_POSITION_FOLLOWING)) break;
      const holder = node.parentElement;
      if (!holder || !shown(holder) || holder.closest(LEFT_OUT) || (skip && skip(holder))) continue;
      const text = clean(node.textContent);
      if (!text) continue;
      const block = holder.closest(BLOCK) || holder;
      const last = blocks[blocks.length - 1];
      if (last && last.el === block) last.text = clean(last.text + ' ' + text);
      else blocks.push({el: block, text});
    }
    return blocks;
  };
  const wordsOf = (label, file) => clean(blocksIn(label, file ? (h) => h.closest('a') : null).map((b) => b.text).join(' '));
  // Whether the page marks a question required without words: a class, or a star
  // drawn by the stylesheet.
  const markedRequired = (el) => {
    for (let node = el, up = 0; node && up < 3; node = node.parentElement, up++) {
      if (/required/i.test(String(node.className))) return true;
      for (const part of ['::after', '::before']) if (/[*✱]/.test(getComputedStyle(node, part).content)) return true;
    }
    return !!el.querySelector('[class*="required" i]');
  };
  const labelsOf = (el) => {
    const found = [];
    if (el.id) found.push(...document.querySelectorAll('label[for="' + quote(el.id) + '"]'));
    if (!found.length && el.closest('label')) found.push(el.closest('label'));
    const by = el.getAttribute('aria-labelledby');
    if (!found.length && by) for (const id of by.split(/\s+/)) { const named = document.getElementById(id); if (named) found.push(named); }
    return found;
  };
  // The question a field or a group of choices answers, a hint given with it, and
  // whether it is marked required: its label, or else the words before it in the
  // nearest part of the page that holds no other field, leaving out its choices'
  // own labels.
  const askedBy = (members) => {
    const first = members[0];
    const file = first.type === 'file';
    const group = members.length > 1 || first.type === 'radio';
    const labels = group ? [] : labelsOf(first);
    let question = clean(labels.map((l) => wordsOf(l, file)).join(' '));
    if (!question && !group && first.getAttribute('aria-label')) question = clean(first.getAttribute('aria-label'));
    if (file && CHOOSING.test(question)) question = '';
    let required = labels.some(markedRequired);
    const theirs = (holder) => {
      const label = holder.closest('label');
      return label && members.some((m) => label.contains(m) || (m.id && label.htmlFor === m.id));
    };
    let blocks = [];
    let box = first.parentElement;
    for (let up = 0; box && box !== document.body && up < 8; up++, box = box.parentElement) {
      if (Array.from(box.querySelectorAll('input, select, textarea')).some((f) => !members.includes(f) && counted(f))) break;
      blocks = blocksIn(box, theirs, first).filter((b) => !marker(b.text));
      if (blocks.length) break;
    }
    if (!question || marker(question) || (question === clean(first.placeholder) && HINT.test(question))) {
      if (blocks.length) {
        question = blocks[0].text;
        required = required || markedRequired(blocks[0].el);
        blocks = blocks.slice(1);
      }
    }
    const said = new Set([question]);
    const hints = [];
    for (const block of blocks) if (!said.has(block.text) && !question.includes(block.text)) { said.add(block.text); hints.push(block.text); }
    const by = first.getAttribute('aria-describedby');
    if (by) for (const id of by.split(/\s+/)) { const d = document.getElementById(id); const text = d && clean(d.innerText); if (text && !said.has(text) && !HINT.test(text)) { said.add(text); hints.push(text); } }
    return {question, hint: clean(hints.join(' ')).slice(0, 400), required: required || MARK.test(question)};
  };
  const said = (b) => clean(b.innerText || b.value || b.getAttribute('aria-label'));
  // A form in a dialog over the page, such as a job board's own application, is
  // read alone, without the page under it. A dialog of tick boxes, such as one
  // about cookies, is no form.
  const asking = 'input:not([type="checkbox"]):not([type="hidden"]):not([type="submit"]):not([type="button"]), select, textarea';
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]'))
    .filter((d) => shown(d) && Array.from(d.querySelectorAll(asking)).some(counted));
  const root = dialogs.length ? dialogs[dialogs.length - 1] : document;
  const fields = [];
  const groups = new Set();
  for (const el of root.querySelectorAll('input, textarea, select')) {
    if (!counted(el)) continue;
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    const name = el.name || '';
    const grouped = type === 'radio' || (type === 'checkbox' && name && document.querySelectorAll('input[type="checkbox"][name="' + quote(name) + '"]').length > 1);
    if (grouped) {
      const key = name || selectorOf(el);
      if (groups.has(key)) continue;
      groups.add(key);
      const members = name ? Array.from(document.querySelectorAll('input[name="' + quote(name) + '"]')).filter(shown) : [el];
      const asked = askedBy(members);
      const field = {
        kind: type === 'radio' ? 'radio' : 'checkboxes',
        label: asked.question, name, id: el.id || '',
        required: asked.required || members.some((m) => m.required || m.getAttribute('aria-required') === 'true'),
        options: members.map((m) => ({label: clean(labelsOf(m).map((l) => wordsOf(l, false)).join(' ')) || m.value, selector: selectorOf(m), checked: m.checked})),
        value: members.filter((m) => m.checked).map((m) => clean(labelsOf(m).map((l) => wordsOf(l, false)).join(' ')) || m.value).join(', '),
        selector: selectorOf(el),
      };
      if (asked.hint) field.hint = asked.hint;
      fields.push(field);
      continue;
    }
    const combobox = el.getAttribute('role') === 'combobox';
    const asked = askedBy([el]);
    const placeholder = clean(el.placeholder);
    let label = asked.question || placeholder;
    const kind = type === 'textarea' ? 'textarea' : el.tagName === 'SELECT' ? 'select' : combobox ? 'combobox' : type === 'checkbox' ? 'checkbox' : type === 'file' ? 'file' : ['email', 'tel', 'url', 'number', 'date'].includes(type) ? type : 'text';
    if (kind === 'file') {
      const around = clean((el.closest('section, fieldset, li, [class*="upload" i], [class*="autofill" i]') || el.parentElement).innerText);
      if (/autofill|auto-fill|parse your r[ée]sum[ée]|import from/i.test(around + ' ' + label)) continue;
      if (!label) label = clean((el.id || name).replace(/[_-]+/g, ' '));
    }
    const field = {
      kind, label, name, id: el.id || '',
      required: asked.required || el.required || el.getAttribute('aria-required') === 'true',
      selector: selectorOf(el),
    };
    if (placeholder && placeholder !== label) field.placeholder = placeholder;
    if (asked.hint) field.hint = asked.hint;
    if (el.maxLength > 0 && el.maxLength < 100000) field.max_length = el.maxLength;
    if (el.pattern) field.pattern = el.pattern;
    if (el.tagName === 'SELECT') {
      field.options = Array.from(el.options).filter((o) => o.value !== '').map((o) => clean(o.text)).filter((t) => t && !/^(select|choose|please select|--)/i.test(t)).slice(0, 5000);
      field.value = el.selectedIndex >= 0 && el.options[el.selectedIndex].value !== '' ? clean(el.options[el.selectedIndex].text) : '';
      field.multiple = el.multiple;
    } else if (kind === 'checkbox') {
      field.value = el.checked;
    } else if (kind === 'file') {
      field.accept = el.accept || '';
      field.value = el.files && el.files.length ? Array.from(el.files).map((f) => f.name).join(', ') : '';
      // What the page shows of a file already chosen, when it keeps the file itself.
      const box = el.closest('[class*="upload" i], [class*="file" i], li, fieldset') || el.parentElement;
      if (!field.value && box) {
        const named = clean(box.innerText).match(/[\w .()-]+\.(pdf|docx?|txt|rtf|odt)\b/i);
        if (named) field.value = named[0].trim();
      }
      // What opens its chooser when clicked: the label it is inside, which a click
      // reaches whatever lies over it; else a button beside it; else a label for it
      // big enough to click, since one for a screen reader alone is a pixel wide.
      const big = (e) => { const box = e.getBoundingClientRect(); return shown(e) && box.width >= 8 && box.height >= 8; };
      let button = null;
      for (let box = el.parentElement, up = 0; box && !button && up < 3; box = box.parentElement, up++) {
        button = Array.from(box.querySelectorAll('button, [role="button"]')).find((b) => big(b) && /attach|upload|choose|browse|select/i.test(said(b)));
      }
      const around = el.closest('label');
      const label = el.id && document.querySelector('label[for="' + quote(el.id) + '"]');
      const chooser = (around && big(around) ? around : null) || button || (label && big(label) ? label : null);
      field.chooser = chooser ? selectorOf(chooser) : selectorOf(el);
    } else if (combobox) {
      // The option chosen, as the drop-down shows it beside its input.
      let shownValue = null;
      for (let box = el.parentElement, up = 0; box && !shownValue && up < 4; box = box.parentElement, up++) {
        shownValue = box.querySelector(VALUE);
        if (!shownValue && box.querySelectorAll('input[role="combobox"]').length > 1) break;
      }
      field.value = clean(shownValue ? shownValue.innerText : '') || el.value || '';
      field.options = [];
    } else {
      field.value = el.value || '';
    }
    fields.push(field);
  }
  const buttons = Array.from(root.querySelectorAll('button, input[type="submit"], [role="button"]')).filter(shown);
  const submit = buttons.find((b) => /\b(submit|send)\b/i.test(said(b)) && !/cancel/i.test(said(b)))
    || buttons.find((b) => b.type === 'submit' && /apply|submit/i.test(said(b)));
  const next = buttons.find((b) => /^(next|continue|save and continue|review)\b/i.test(said(b)));
  const frames = Array.from(document.querySelectorAll('iframe[src]')).map((f) => f.src)
    .filter((src) => /greenhouse\.io|lever\.co|ashbyhq|myworkdayjobs|smartrecruiters|icims|jobvite|bamboohr|workable/i.test(src) && !/captcha/i.test(src));
  // A check that the user is a person, shown for them to do and not yet done: not
  // one that runs unseen, whose badge only says the page is protected. Once the
  // user has done it, the page holds its response, and it asks no more.
  const done = Array.from(document.querySelectorAll('[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]'))
    .some((f) => (f.value || '').length > 20);
  const challenge = !done && Array.from(document.querySelectorAll('iframe')).some((f) => {
    const src = f.src || '';
    if (!/captcha|challenge|turnstile/i.test(src + ' ' + (f.title || ''))) return false;
    if (/size=invisible|checkbox-invisible/i.test(src)) return false;
    const box = f.getBoundingClientRect();
    return shown(f) && box.width > 30 && box.height > 30 && box.bottom > 0 && box.right > 0;
  });
  const errors = Array.from(document.querySelectorAll('[role="alert"], [class*="error" i]'))
    .filter((e) => shown(e) && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName)).map((e) => clean(e.innerText)).filter((t) => t && t.length < 300);
  return {
    url: location.href,
    title: document.title,
    fields,
    submit: submit ? {selector: selectorOf(submit), said: said(submit)} : null,
    next: next ? {selector: selectorOf(next), said: said(next)} : null,
    frames,
    password: !!Array.from(document.querySelectorAll('input[type="password"]')).find(shown),
    challenge,
    errors: Array.from(new Set(errors)).slice(0, 12),
  };
}"""

# The options a drop-down shows while it is open: those of the list it controls, or
# else every option shown on the page.
OPTIONS = r"""() => {
  const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
  const shown = (el) => el.getClientRects().length > 0;
  const field = document.activeElement;
  const owned = field && (field.getAttribute('aria-controls') || field.getAttribute('aria-owns'));
  const list = owned && document.getElementById(owned.split(/\s+/)[0]);
  const options = Array.from((list || document).querySelectorAll('[role="option"]')).filter(shown);
  return options.map((o) => clean(o.innerText || o.textContent)).filter(Boolean).slice(0, 5000);
}"""

# Marks the option shown with exactly these words, for the browser to click. Returns
# whether there was one.
PICK = r"""(wanted) => {
  const clean = (text) => (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
  for (const old of document.querySelectorAll('[data-orbit-pick]')) old.removeAttribute('data-orbit-pick');
  const option = Array.from(document.querySelectorAll('[role="option"]'))
    .find((o) => o.getClientRects().length > 0 && clean(o.innerText || o.textContent) === clean(wanted));
  if (!option) return false;
  option.setAttribute('data-orbit-pick', '1');
  return true;
}"""
PICKED = '[data-orbit-pick="1"]'


def pick(wanted: str) -> str:
    """The script that marks the option shown with exactly these words."""
    return f"() => ({PICK})({json.dumps(wanted)})"
