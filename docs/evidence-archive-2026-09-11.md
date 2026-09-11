# 历史证据冷归档（2026-09-11）

## 范围与授权

本次仅执行“评估仓库精简优化方案”的第 1 项：证据归档与重复内容核验。目标是在保留研究来源、失败记录和恢复能力的前提下减少本地占用。第 2–4 项未开始。

输入限定为 `processed/final_test_evidence/`、`processed/final_test_incidents/` 和 `processed/final_test_evidence_incidents/` 的 18 个历史目录。原始 Data、Stage 5–8 release 与其输入、候选工作区及研究代码保持原状。未解析或评价 2022–2025 策略结果，未创建 token、attempt、CURRENT 或执行 resume。

## 存储与恢复

全部历史文件保存在本地 `artifacts/evidence_archive/2026-09-11/evidence.zip`，相同 SHA-256 内容只存储一次。嵌入清单保存全部原路径、大小、哈希、文件权限和修改时间，另存目录权限和修改时间。恢复时为重复内容创建独立文件。

这属于存储位置调整，不改变任何历史证据的有效性、失败状态或复用限制。原目录的 `COLD_ARCHIVE.md` 是占位说明，不能作为完整事故现场进行核验。历史文档中的旧路径仍指原始身份路径；它们的字节内容现在需要从归档恢复。

使用现有 Homebrew Python，将归档恢复到一个**尚不存在的新目录**：

```sh
/opt/homebrew/bin/python3 artifacts/evidence_archive/2026-09-11/restore.py /absolute/path/to/new-restore-directory
```

恢复工具先核对整个归档 SHA-256，然后恢复每个文件并逐一校验哈希；拒绝覆盖已有目的目录及越界路径。恢复整个目录需要约 23 GiB 可用空间，建议预留 25 GiB 以上。归档及其同目录的 verification.json、restore.py 应一起保留。

若未来需要恢复到项目原位置，应先在新目录恢复并验证，再检查原位置仅有本次占位标记、没有新增研究状态，受控切回这三个目录；不得将历史现场与新状态合并覆盖。恢复字节不构成重新采集、重入失败 attempt、复用 token 或启动 v2.0 的授权。

归档契约包括文件内容、路径、空目录、POSIX 权限和纳秒修改时间，不包含 ACL、扩展属性、创建时间或文件系统 flags。不能用重新下载或重跑保证历史字节一致，因此不将这些历史证据标为可随意再生。

## 证据索引

本地产物目录：`artifacts/evidence_archive/2026-09-11/`（Git 忽略；包含历史证据，不上传）。

- `manifest.json`：逐文件清单与原路径。
- `directory_metadata.json`：目录权限与修改时间。
- `duplicate_groups.json`：重复组、原路径和逻辑冗余量。
- `references.json`：文档、配置、源码、工作区及发布血缘的文本引用与 CURRENT 快照。
- `retention.json`：各历史目录用途、保留及重建判断；每个文件继承其目录策略。
- `verification.json`：归档哈希及全部唯一内容解压核验。
- `full_restore_verification.json`：完整目录恢复与元数据核验。
- `sources_reverified.json`：移除前第二次逐文件 SHA-256 复核。
- `retirement.json`：展开副本移除记录。
- `releases_before.json`、`releases_after.json`：四个权威发布的逐文件核验。
- `restore.py`、`test_restore.py`：恢复工具与人工夹具验证。

引用检索是显式文本引用检查，并非所有动态依赖的形式化证明。Stage 5–8 当前发布的全部 183 个清单文件独立保留和核验；历史事故重入核验及未来 Stage 9 工作必须先恢复历史路径。

## 执行验收

- [x] 清单：467,112 个文件，21.66 GiB 逻辑内容。
- [x] 重复核验：269,273 份唯一内容，重复逻辑字节 11.32 GiB。
- [x] 去重压缩归档：8.38 GiB。
- [x] 全部唯一对象解压落盘校验；完整恢复 467,112 个原路径，逐文件 SHA-256、文件和目录权限及修改时间核验通过。
- [x] 移除前全部原件再次 SHA-256 核验，目录条目无新增或变更。
- [x] 移除全部展开副本及恢复演练临时目录，三个根和 18 个历史目录保留冷归档占位标记。
- [x] 当前 Stage 5–8 四个 release 的 183 个文件前后哈希核验通过，CURRENT 字节不变，最终测试 CURRENT 不存在。
- [x] 恢复工具人工夹具覆盖独立重复文件、二进制字节、空目录、权限、mtime、拒绝覆盖、路径越界和压缩包损坏。
- [x] 文档、保留策略、引用索引及 HANDOFF 已写入；不推进第 2 项。

连同逐文件索引、恢复工具和核验记录，本次按逻辑大小净减少约 **12.97 GiB**（最终小型回执带来的差异不足 0.01 GiB）。这不是 APFS 物理释放量测量；共享块、快照及文件系统分配会影响实际可用空间。初始目录占用统计与逐文件逻辑字节不是同一个口径。

归档 SHA-256：`2d2f574c319ff4dbb32b8fbd3e82c0d4ce2fbc30027ff15f8141a09a72871d55`。

本次没有修改研究代码或参数，因此验证集中于真实归档恢复、源字节一致性和现行发布完整性，没有重跑研究回测或全量 pytest。
