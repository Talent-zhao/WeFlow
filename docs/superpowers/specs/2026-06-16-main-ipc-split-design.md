# Design: main.ts IPC Handler 拆分

## 目标

将 main.ts 中 ~2800 行 IPC handler 注册代码按功能域拆分到 `electron/handlers/` 目录，减少单文件行数（4645 → ~1800），提高可维护性，零行为变更。

## 方法

保守拆分：只移动代码位置，不改业务逻辑、API signature 或运行时行为。

## Handler 文件划分

| 文件 | IPC channels | 来源行号 |
|---|---|---|
| `handlers/chat.ts` | chat:* (~30个) | 2420-2793 |
| `handlers/export.ts` | export:* | 3204-3557 |
| `handlers/sns.ts` | sns:* (~15个) | 2795-3002 |
| `handlers/analytics.ts` | analytics:*, groupAnalytics:*, annualReport:*, dualReport:*, cache:* (~25个) | 3559-4100 |
| `handlers/window.ts` | window:*, notification-clicked (~15个) | 767, 2178-2344, 3697-3743 |
| `handlers/app.ts` | app:*, config:*, log:*, cloud:*, persona:*, dialog:*, shell:* (~20个) | 1779-1797, 1954-2069, 4227-4312 |
| `handlers/db.ts` | dbpath:*, wcdb:*, backup:*, key:* (~12个) | 2365-2418, 4122-4145 |
| `handlers/media.ts` | image:*, video:*, whisper:* (~8个) | 2346-2363, 3004-3133, 3623-3634, 4305-4340 |
| `handlers/insight.ts` | insight:*, groupSummary:* (~16个) | 1800-1926 |
| `handlers/misc.ts` | auth:*, bot:*, http:*, social:*, diagnostics:* (~20个) | 1928-1952, 2038-2057, 3135-3202, 4147-4225 |

## 依赖注入

```ts
// electron/handlers/types.ts
export interface HandlerDeps {
  mainWindow: BrowserWindow | null;
  getMainWindow: () => BrowserWindow | null;
  configService: ConfigService | null;
  // ... 各 service 按需添加
}
```

每个 handler 文件暴露:
```ts
export function registerHandlers(deps: HandlerDeps): void
```

## main.ts 变化

- 保留: imports, auto-update, 窗口管理函数, export worker, launch-at-startup
- 新增: 集中注册区，调用各 `registerHandlers(deps)`
- 移除: 全部 ipcMain.handle/on 及其内联 handler 实现
- 保留在 main.ts: 顶层状态变量 (activeExportWorkers, configService 等) 作为 deps 传入

## 验证

- `tsc --noEmit` 类型检查通过
- `npm run dev` 应用正常运行
- 不引入任何功能回归
