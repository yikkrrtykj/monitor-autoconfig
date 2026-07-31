const assert = require("assert");
const path = require("path");

const {
  pageCount,
  clampPageIndex,
  createIspCarousel
} = require(path.resolve(__dirname, "../bigscreen/isp-carousel.js"));

assert.deepStrictEqual(
  [0, 1, 2, 3, 4, 5].map((count) => pageCount(count)),
  [0, 1, 1, 2, 2, 3]
);
assert.strictEqual(clampPageIndex(-1, 5), 0);
assert.strictEqual(clampPageIndex(9, 5), 2);

for (let count = 0; count <= 5; count += 1) {
  const items = Array.from({ length: count }, (_, index) => `isp-${index + 1}`);
  const pager = createIspCarousel();
  pager.updateTotal(count);
  pager.activate();
  assert.strictEqual(pager.snapshot().pageCount, Math.ceil(count / 2));
  assert.deepStrictEqual(pager.visibleItems(items), items.slice(0, 2));
  while (pager.snapshot().canNext) pager.move(1);
  const finalSize = count ? (count % 2 || 2) : 0;
  assert.strictEqual(pager.visibleItems(items).length, finalSize);
  pager.deactivate();
}

let nextTimerId = 1;
const timers = new Map();
const cleared = [];
const pageChanges = [];
const carousel = createIspCarousel({
  pageSize: 2,
  intervalMs: 10000,
  setIntervalFn(callback, delay) {
    const id = nextTimerId++;
    timers.set(id, { callback, delay });
    return id;
  },
  clearIntervalFn(id) {
    cleared.push(id);
    timers.delete(id);
  },
  onPageChange(state) {
    pageChanges.push(state.pageIndex);
  }
});

carousel.updateTotal(5);
carousel.activate();
assert.deepStrictEqual(carousel.visibleItems(["a", "b", "c", "d", "e"]), ["a", "b"]);
assert.strictEqual(timers.size, 1);
assert.strictEqual([...timers.values()][0].delay, 10000);

const initialTimerId = [...timers.keys()][0];
carousel.updateTotal(5);
assert.deepStrictEqual([...timers.keys()], [initialTimerId], "数据刷新不能重置轮播计时器");

timers.get(initialTimerId).callback();
assert.deepStrictEqual(carousel.visibleItems(["a", "b", "c", "d", "e"]), ["c", "d"]);
assert.deepStrictEqual(pageChanges, [1]);

carousel.move(1);
assert.deepStrictEqual(carousel.visibleItems(["a", "b", "c", "d", "e"]), ["e"], "最后单数页只显示剩余一项");
assert.strictEqual(carousel.snapshot().pageIndex, 2);
assert.strictEqual(timers.size, 1);
assert.notStrictEqual([...timers.keys()][0], initialTimerId, "手动翻页应重新计算十秒");

carousel.move(1);
assert.strictEqual(carousel.snapshot().pageIndex, 2, "下一页不能越过最后一页");
carousel.move(-9);
assert.strictEqual(carousel.snapshot().pageIndex, 0, "上一页不能越过第一页");

const restartedTimerId = [...timers.keys()][0];
timers.get(restartedTimerId).callback();
timers.get(restartedTimerId).callback();
timers.get(restartedTimerId).callback();
assert.strictEqual(carousel.snapshot().pageIndex, 0, "自动轮播应从最后一页回到第一页");

carousel.deactivate();
assert.strictEqual(timers.size, 0, "离开比赛页必须停止轮播");
assert.strictEqual(carousel.snapshot().pageIndex, 0);
carousel.activate();
assert.strictEqual(carousel.snapshot().pageIndex, 0, "返回比赛页从第一页开始");

carousel.updateTotal(2);
assert.strictEqual(timers.size, 0, "一页以内不应启动轮播");

console.log("bigscreen ISP carousel tests passed");
