import { applyDiscount } from "./discount";
import { fmt } from "@lib/util";
import { hello } from "fakepkg";
import { legacyHelper } from "./legacy.js";

const handlers: Record<string, () => void> = {};

export class OrderService {
  checkout(total: number) {
    const d = applyDiscount(total);
    return fmt(legacyHelper(d));
  }
  run(name: string) {
    handlers[name]();
    hello();
  }
}
