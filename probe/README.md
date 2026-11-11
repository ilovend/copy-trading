# probe/

这个目录存放**本地会话**和调研脚本，默认不进版本库：

| 文件 | 说明 |
|---|---|
| `session.json` | 币安网页登录态（`bot/update_session.py` 自动生成） |
| `curl*.txt` | 临时保存的 cURL（可选） |

`session.json` 等同账号权限，**不要提交、不要外发**。

目录不存在时程序会自动创建；如果你是下载的仓库，第一次运行
「更新登录态」或 `python bot\update_session.py --clipboard` 就会生成。
