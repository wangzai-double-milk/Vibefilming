# step 1. 用户输入
```text
生成大约60秒水墨武侠，电影预告质感，动作流畅，少量对白。剧情为：身负血海深仇的男子执于以杀止乱、重塑江湖，被魔气反噬深陷执念魔障。
```

### 1.1 自动根据用户query写分剧本和分镜（节选）
<div style="display: flex; gap: 10px; justify-content: center;">
  <img width="400" alt="image" src="https://github.com/user-attachments/assets/51b3f81b-3f73-447d-8fe8-bee1cc13dbef" />
  <img width="400" alt="image" src="https://github.com/user-attachments/assets/5fe27a74-bb8b-489a-b22a-d9a4ff1b6aaf" />
</div>

### 1.2 自动生成关键角色图和场景，以及分镜故事板
<img width="640" alt="image" src="https://github.com/user-attachments/assets/d555f809-6c5a-4974-a500-7c6f6ca8d4f9" />

### 1.3 自动进行提示词优化，生成视频片段
```text
将<图片1>中的黑衣长发冷峻剑客定义为<主体1>（无名剑客），将<图片2>中的水墨竹林场景定义为场景。
镜头一：远景缓推，竹林深处雾气弥漫，无名剑客闭目静立于竹林中，风吹竹叶沙沙作响。
镜头二：中景，无名剑客猛然睁眼，眼神凌厉如刀，右手缓缓拔剑出鞘，剑锋划破空气带起墨色飞溅。
镜头三：远景，竹林深处一道黑影快速掠过，无名剑客持剑而立，衣袂随风飘动，竹叶纷纷飘落。
<风吹竹叶的沙沙声><剑锋出鞘>
```

https://github.com/user-attachments/assets/d038992f-3cf5-45e9-9f86-1d34e478019e

### 1.4 自动写prompt调用工具审核每一个生成的中间产物（人物素材和中间视频节选）
<div style="display: flex; gap: 10px; justify-content: center;">
  <img width="400" alt="image" src="https://github.com/user-attachments/assets/a5ed8e6b-cc9d-4330-b5bc-d268cacaaf05" />
  <img width="400" alt="image" src="https://github.com/user-attachments/assets/500ae82f-0330-4ff0-a91f-e67077807aca" />
</div>

### 1.5 所有内容审核通过后，自动合成成片
从始至终，用户只需要开头的50字要求
项目演示视频：[视频过大，点击下载播放](https://github.com/wangzai-double-milk/Vibefilming/releases/download/v1-video/_._v1.mp4)

# step 2. 通过对话调整成片
### 2.1 用户输入
```text
人物拿剑的手穿模了，帮我改一下。然后好像转场有点生硬这个也改一下吧。
```
<img width="640" alt="image" src="https://github.com/user-attachments/assets/1bfb7d68-81e9-41ca-9a78-021f47aa8b01" />

### 2.2 自动寻找穿模片段
<img width="640" alt="image" src="https://github.com/user-attachments/assets/6a79a667-4aad-4e33-a1df-fd417100a2ff" />
<img width="640" alt="image" src="https://github.com/user-attachments/assets/bf7f1777-37cd-45ff-82ca-d6afe11bfefd" />

### 2.3 自动修复关键帧，并且认为触及视频模型上限时，会尝试其他方案
尝试修复关键帧

<img width="400" alt="image" src="https://github.com/user-attachments/assets/4fe01b83-062d-45e4-8829-0a548fc843c8" />
<img width="400" alt="image" src="https://github.com/user-attachments/assets/1af7e112-58ee-41a9-aa65-cd80800ed9b2" />

发现受制于模型能力无法解决时，就会采取别的方式：
更改镜头彻底解决手部问题

<img width="640" alt="image" src="https://github.com/user-attachments/assets/7813c9fd-a545-4ca5-86ec-e75e0947407f" />

### 2.4 重新生成与修复帧相关内容，合成成片
项目演示视频：[视频过大，点击下载播放](https://github.com/wangzai-double-milk/Vibefilming/releases/download/v1-video/_._v4_bgm.mp4)
