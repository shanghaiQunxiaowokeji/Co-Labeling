# 视频暂停圈选标注工具

播放视频 → 人工暂停并圈选区域 → 记录标注 → 合成新视频：
**每个人工标记处画面冻结 1 秒（可调），并画出人工圈选的区域，音频同步插入等长静音。**

## 依赖

- Python 3.9+：`opencv-python`、`Pillow`（标准库 `tkinter`）
- [ffmpeg](https://ffmpeg.org/)（需要 `ffmpeg` 与 `ffprobe` 在 PATH 中）

```powershell
pip install -r requirements.txt
```

## 1. 标注

```powershell
python annotator.py "..\..\腾讯——多模态难题\Hanoi2.webm"
```

不带参数运行会弹出文件选择框。

| 操作 | 说明 |
| --- | --- |
| 空格 | 播放 / 暂停 |
| ← / → | 前后一帧 |
| Shift + ← / → | 前后一秒 |
| 鼠标左键拖拽 | 圈选（播放中拖拽会自动先暂停） |
| 鼠标右键 / Ctrl+Z | 撤销当前帧最后一笔 |
| Del | 删除选中（或当前帧）的标记点 |
| Ctrl+S | 保存标注 |

- 圈选工具：矩形、椭圆、自由笔、箭头；可设颜色、线宽、文字标签。
- 同一帧可以画多个图形，它们合并成一个“标记点”。
- 「停顿秒数」决定该标记点在新视频里冻结多久，默认 1 秒；画完后可选中标记点用
  「应用停顿秒数到选中标记」修改。
- 标注默认保存到视频同目录的 `<视频名>.annotations.json`，下次打开同一视频会自动载入。
- 右侧「导出新视频…」会先保存标注，再调用 `export_video.py` 并显示进度日志。

## 2. 导出

也可以单独用命令行导出：

```powershell
python export_video.py "Hanoi2.annotations.json" -o "Hanoi2_annotated.mp4"
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--output / -o` | 输出路径，后缀 `.mp4`（H.264，快）或 `.webm`（VP9，慢很多） |
| `--hold` | 标注未指定时的默认停顿秒数（默认 1.0） |
| `--crf` / `--preset` | H.264 画质与速度（默认 20 / veryfast） |
| `--jobs` | 并行编码的片段数（默认 2） |
| `--dry-run` | 只打印合成计划，不编码 |
| `--keep-temp` | 保留中间文件便于排查 |
| `--video` | 覆盖标注里记录的源视频路径（视频被移动时用） |

## 实现说明

- 播放器用 OpenCV 在后台线程解码，UI 线程只负责显示，支持逐帧步进与拖动时间轴。
- 圈选坐标以**原始视频像素**保存，与显示缩放无关。
- 导出按标记点把原片切成若干段分别重编码为 MPEG-TS，冻结帧单独编码成等长片段
  （视频为静止画面 + `anullsrc` 静音），最后用 concat 解复用器拼接。
  相比一次性 `filter_complex`（trim+split+concat），长视频不会出现内存爆涨。
- 冻结处使用的正是被标记的那一帧，冻结结束后原片从该帧继续播放。

## 标注文件格式

```jsonc
{
  "version": 1,
  "video": "D:/path/Hanoi2.webm",
  "fps": 29.97, "width": 1920, "height": 1080, "frame_count": 12459,
  "marks": [
    {
      "frame": 150,            // 标记帧号
      "time": 5.005,           // 对应秒数（只读参考）
      "hold": 1.0,             // 冻结秒数
      "shapes": [
        {
          "type": "rect",      // rect | ellipse | freehand | arrow
          "points": [[600, 300], [1300, 800]],
          "color": "#FF3B30",
          "width": 6,
          "label": "关键步骤"
        }
      ]
    }
  ]
}
```
