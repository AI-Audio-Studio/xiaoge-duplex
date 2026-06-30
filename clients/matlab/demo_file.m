function demo_file(host, port, inWav, outWav)
%DEMO_FILE  无声卡 demo:经 TCP 桥把 wav 发给小歌,收回的音频存成 wav(B 方案)。
%   先起桥:  python bridge/xiaoge_bridge.py <host> <port> --up 5001 --down 5002
%   再运行:  demo_file('127.0.0.1', 8787, 'in.wav', 'out.wav')
%   in.wav 须 16kHz/单声道/16-bit。状态:未在交付环境运行,按 README 验证。
    if nargin < 4, outWav = 'xiaoge_reply.wav'; end

    [x, fs] = audioread(inWav, 'native');
    assert(fs == 16000 && size(x, 2) == 1 && isa(x, 'int16'), ...
        'in.wav 必须是 16kHz/单声道/16-bit');

    up = tcpclient(host, 5001, 'Timeout', 1);
    down = tcpclient(host, 5002, 'Timeout', 1);
    cleanup = onCleanup(@() clear('up', 'down')); %#ok<NASGU>

    frame = 320;                       % 20ms@16k
    received = int16([]);
    for i = 1:frame:numel(x)
        seg = x(i:min(i + frame - 1, numel(x)));
        write(up, seg, 'int16');
        pause(0.02);                   % 按实时速率发
        received = drain(down, received);
    end
    t0 = tic;                          % 收尾巴 ~3s
    while toc(t0) < 3
        received = drain(down, received);
        pause(0.05);
    end

    audiowrite(outWav, received, 16000);
    fprintf('已发送 %d 样本,收到 %d 样本 → %s\n', numel(x), numel(received), outWav);
end

function buf = drain(down, buf)
    k = floor(down.NumBytesAvailable / 2);
    if k > 0
        buf = [buf; read(down, k, 'int16')];
    end
end
