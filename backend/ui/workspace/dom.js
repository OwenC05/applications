import {codePoints} from './client.js';

// All source, profile and model strings become text nodes, never executable HTML.
export function node(tag, text = '', className = '') {
  const element = document.createElement(tag);
  element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
export function field(name, label, {value = '', type = 'text', required = false, maxLength, options, rows = 4} = {}) {
  const wrapper = node('label');
  const control = document.createElement(options ? 'select' : type === 'textarea' ? 'textarea' : 'input');
  control.name = name;
  control.required = required;
  if (options) for (const [id, text] of options) control.append(new Option(text, id));
  else if (type === 'textarea') control.rows = rows;
  else control.type = type;
  if (type === 'number') { control.min = '1'; control.step = '1'; }
  if (type === 'checkbox') { control.checked = Boolean(value); wrapper.append(control, document.createTextNode(label)); }
  else {
    // Preserve the native first-option default when no explicit matching value
    // exists. Setting an unmatched empty value otherwise deselects every option.
    control.value = options && !options.some(([id]) => id === (value ?? '')) ? options[0]?.[0] ?? '' : value ?? '';
    wrapper.append(node('span', label), control);
  }
  if (maxLength) {
    const counter = node('span', '', 'muted');
    const validate = () => {
      const count = codePoints(control.value);
      control.setCustomValidity(count > maxLength ? `Use at most ${maxLength} Unicode characters (code points).` : '');
      counter.textContent = `${count} / ${maxLength} characters (code points)`;
    };
    control.dataset.maxCodePoints = String(maxLength);
    control.addEventListener('input', validate);
    validate(); wrapper.append(counter);
  }
  return wrapper;
}
export function section(title, ...children) {
  const result = node('section'); result.append(node('h2', title), ...children); return result;
}
export function text(text, className = '') { return node('p', text, className); }
export function link(url, title) { const result = node('a', title); result.href = url; if (url.startsWith('https:')) result.rel = 'noopener noreferrer'; return result; }
