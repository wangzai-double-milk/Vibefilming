"""依次运行全部 smoke test 并汇总。"""
import importlib
import sys
import time

TESTS = [
    ("test_01_doubao_text", "Doubao 文本"),
    ("test_02_doubao_vlm", "Doubao VLM"),
    ("test_03_seedream_t2i", "Seedream 文生图"),
    ("test_04_seedream_i2i", "Seedream 图生图"),
    ("test_05_seedance_t2v", "Seedance 文生视频"),
    ("test_07_genbgm", "豆包 GenBGM 音乐"),
    ("test_08_vod", "VOD 视频点播"),
]


def main():
    results = []
    for mod_name, label in TESTS:
        t0 = time.time()
        try:
            mod = importlib.import_module(mod_name)
            ok = bool(mod.main())
        except Exception as e:
            print(f"  ❌ 模块异常：{e}")
            ok = False
        dt = time.time() - t0
        results.append((label, ok, dt))

    print("\n" + "=" * 70)
    print("  汇总")
    print("=" * 70)
    for label, ok, dt in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status}  {label:<24}  ({dt:.1f}s)")
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"\n  {n_ok} / {len(results)} 通过")


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
