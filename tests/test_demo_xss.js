// Chạy bằng node tests/test_demo_xss.js. DOM stub từ chối mọi HTML động.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children = []; this.value = ''; this.classList = {add(){}, remove(){}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  set innerHTML(value) { throw new Error('Dynamic innerHTML: ' + value); }
}
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const html = fs.readFileSync('src/repair_estimate_api/static/demo.html', 'utf8');
const source = html.slice(html.indexOf('function renderResult('), html.indexOf('// Filter tabs'));
const context = vm.createContext({$, document: {createElement: () => new Element()},
  DOC_LABELS: {vin: 'VIN'}, CAT_LABELS: {}, currentFilter: 'all', currentResult: null});
vm.runInContext(source, context);
const attack = '<img src=x onerror="alert(1)"><svg onload="alert(2)">';
context.renderResult({document: {vin: attack}, metrics: {}, items: [{raw_code: attack, raw_name: attack, quantity: 1, material_amount: 1500, labor_amount: 0,
  category: attack, mapping: {matches: [{name: attack, code: attack}]}}]});
const card = $('docGrid').children[0];
assert.equal(card.children[1].textContent, attack);
assert.equal(card.children[1].title, attack);
const row = $('tableRows').children[0];
assert.equal(row.children[1].textContent, attack);
assert.equal(row.children[0].textContent, attack);
assert.equal(row.children[3].textContent, '1.500');
assert.equal(row.children[4].textContent, '0');
assert.equal(row.children[5].textContent, '—');
assert.equal(row.children[12].children[0].children[0].textContent, attack);
assert.equal(row.children[12].children[0].children[1].textContent, attack);
console.log('XSS regression: OCR/document/category/codebook payloads remain text');
