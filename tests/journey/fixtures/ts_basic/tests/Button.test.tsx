import { describe, it, expect } from "vitest";
import { Button } from "../src/components/Button";

describe("Button", () => {
  it("renders children", () => {
    const node = Button({ children: "Hi", onClick: () => {} });
    expect(node).toBeDefined();
  });
});
