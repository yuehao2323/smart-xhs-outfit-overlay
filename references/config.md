# 配置协议 v1

参考 examples/ootd.json 获取可运行实例。JSON 使用 UTF-8；相对 image 路径以配置文件目录为基准。

必填：
- version: 1。
- image: 原图路径。
- source_sha256、source_size: inspect 结果；尺寸已 EXIF 转正。
- post_type: 用户帖子类型。
- analysis.people: 非空数组；每个人物含 bbox 和 faces 数组。所有框均为 [x0,y0,x1,y1]，0..1 归一化；人物框完整覆盖人物、衣服和鞋，不只是脸部。
- boxes: 每项包含唯一 id、role、text。role 恰好一个 title、1~3 个 note、0~1 个 extra。

可选：
- sentence_lines: true（默认）。显式换行/句末标点分隔短句，每句保留为完整一行，去掉行尾句号；过长时拒绝硬拆，需宿主精简。editable.json 和 copy.txt 同步保存处理后的文案。false 恢复按字体宽度自动拆行，供旧版配置兼容。
- uniform_font: true（默认）。标题、说明、补充共同使用约图宽 2.3% 的字号，必要时全部同步缩小，最低约图宽 1.8%；false 恢复按角色设定字号。
- distribution: balanced（默认）。纵向中心等距、左右交替，遇到保护区时自动避让；grouped 恢复说明与补充上下紧接的布局。显式 anchor 仍优先作为位置偏好，均衡模式通常省略 anchor。
- preset: compact-notes（默认，紧凑直角文字批注）或 cards（早期圆角疏散卡片）。前者按角色选黑白灰底，说明与补充优先同列紧接；后者按局部亮度自动选黑白。
- preferences: {tone, length, focus}，保留用户偏好供后续改稿。
- analysis.items: [{name, color, bbox, observation}]，仅记录看得见的内容。
- analysis.style、analysis.mood、analysis.uncertainties: 字符串或数组，供人工复核。
- analysis.protected_regions: 额外硬避让框（包袋、饰品、图片自带重要字等）。
- analysis.whitespace: 适合放字的区域数组，程序给区域内候选框加分。不会覆盖人物保护。
- boxes[].style: auto / black / white / gray-white / gray-black / palette。
- boxes[].anchor: [x,y]，偏好的文字框左上角；自动排版仍会调整。
- boxes[].rect: 固定框 [x0,y0,x1,y1]。会自动换行并在限定范围缩小字号，无法放下或发生遮挡即报错；有效可见框高度可能收紧到文字实际高度。固定框不得接触人物或其他框。
- fallback: panel（默认）或 error。自动布局无安全空间时 panel 扩展右侧说明区；error 报错。若设置了固定 rect，则错误不会被自动侧栏掩盖。
- caption: 自动生成的帖子正文，保存至 copy.txt。
- source_credit: 测试素材署名/来源，原样保存在 editable.json。

全人物框和额外保护框都是硬约束；视觉模型应对估计坐标外扩留余量。程序另留安全间隙（compact-notes 约图宽 0.8%，cards 约 1.4%），并检测框间相交。不要缩小保护框来迁就排版。程序仅依据提供的视觉分析做避让，不包含独立人脸检测或分割模型。

图面可读性：compact-notes 标题字号约图宽 3.4%、说明约 2.3%，内边距约字号 10%、行高约 1.1 倍字号、直角；cards 标题约 3.9%、说明约 2.3%，内边距约字号 48%、行高约 1.38 倍字号、圆角。最低标题 2.6%、说明 1.8%，并设像素下限。按实际字体 advance/ink 宽度换行，逐字号测量行高和 padding，无法容纳时拒绝生成，而非裁剪文字。

自动样式取局部平均亮度；palette 取局部量化主色、限制饱和度 20%。保持原图色彩，不为“低饱和排版”给原照片加滤镜。
