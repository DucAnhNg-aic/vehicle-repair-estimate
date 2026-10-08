// Chạy bằng node tests/test_demo_tabs.js. Kiểm tra khung kết quả/trạng thái không lẫn giữa hai tab.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/repair_estimate_api/static/demo.html', 'utf8');
const source = html.slice(html.indexOf('let activeTab'), html.indexOf('function showTab('));
const screen = {result: null, status: null};
const context = vm.createContext({
  renderResult: data => { screen.result = data; },
  clearResult: () => { screen.result = null; },
  setJobStatus: (...args) => { screen.status = args; },
});
vm.runInContext(source + '; this.api = {setStatus, showResult, restoreTabView, setTab: n => { activeTab = n; }};', context);
const {setStatus, showResult, restoreTabView, setTab} = context.api;

setStatus('analyze', 'running', 'đang OCR', '10s');             // job đang chạy ở tab Phân tích
setTab('history'); restoreTabView('history');                    // mở tab Lịch sử
assert.equal(screen.result, null);
assert.equal(screen.status[0], 'idle');                          // không thấy trạng thái job
showResult('history', {run: 'cũ'}); setStatus('history', 'success', 'xem lịch sử');
setStatus('analyze', 'running', 'vẫn đang OCR', '20s');           // job cập nhật lúc đang xem lịch sử
assert.deepEqual(screen.result, {run: 'cũ'});                    // không bị vẽ đè
assert.equal(screen.status[1], 'xem lịch sử');
showResult('analyze', {job: 'mới'});                              // job xong khi vẫn ở tab Lịch sử
assert.deepEqual(screen.result, {run: 'cũ'});
setStatus('analyze', 'success', 'xong', '30s');
setTab('analyze'); restoreTabView('analyze');                    // quay lại tab Phân tích
assert.deepEqual(screen.result, {job: 'mới'});                   // thấy kết quả OCR, không phải của lịch sử
assert.deepEqual(screen.status, ['success', 'xong', '30s']);
setTab('history'); restoreTabView('history');
assert.deepEqual(screen.result, {run: 'cũ'});                    // lịch sử vẫn được nhớ
console.log('Tab views: kết quả và trạng thái của hai tab không lẫn nhau');
