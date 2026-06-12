import assert from "node:assert/strict";
import { clamp } from "../src/utils/clamp.ts";
import { formatMoney } from "../src/utils/format_money.ts";
import { slugify } from "../src/utils/slugify.ts";
import { truncateText } from "../src/utils/truncate_text.ts";

assert.equal(clamp(5, 1, 10), 5);
assert.equal(clamp(-5, 1, 10), 1);
assert.equal(clamp(50, 1, 10), 10);
assert.equal(formatMoney(123456), "USD 1234.56");
assert.equal(formatMoney(-5), "-USD 0.05");
assert.equal(slugify("Hello,  World!"), "hello-world");
assert.equal(truncateText("abcdef", 4), "abc…");
console.log("ok: 7 assertions");
