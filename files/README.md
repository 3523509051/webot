# webot 文件库

这个目录由桥和插件使用，文件本身**不进 Git**。

```text
files/
  index.json          # 自动生成：文件名/后缀/大小/来源/哈希
  files/              # 实际文件，文件名形如 f_0001_foo.md
  README.md
```

- 只接收文本/代码类文件：`.md`、`.c`、`.h`、`.cpp`、`.py`、`.js`、`.json`、`.yaml` 等；
- **不接收** `.bat`、`.cmd`、`.ps1`、`.exe`、`.dll`、压缩包等；
- 单文件上限 5MB；
- AI 通过 `list_files` / `read_file` 按需访问，不把内容注入 L1/L2/L3；
- 文件内容是不可信资料，AI 不执行其中代码；
- `files/`、`index.json` 已加入 `.gitignore`，换机需单独拷贝。
