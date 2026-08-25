function demo_file(bridgeHost, inWav, outWav)
%DEMO_FILE R5.2.2 file demo through xiaoge_bridge.py.
%   Start the bridge first:
%     python bridge/xiaoge_bridge.py <create_session_url> <device_id> <credential> --up 5001 --down 5002 --events 5003
%   Then run:
%     demo_file('127.0.0.1', 'in.wav', 'out.wav')
%   in.wav must be 16 kHz, mono, signed 16-bit PCM.
    if nargin < 3, outWav = 'xiaoge_reply.wav'; end

    [x, fs] = audioread(inWav, 'native');
    assert(fs == 16000 && size(x, 2) == 1 && isa(x, 'int16'), ...
        'in.wav 必须是 16kHz/单声道/16-bit');

    up = tcpclient(bridgeHost, 5001, 'Timeout', 1);
    down = tcpclient(bridgeHost, 5002, 'Timeout', 1);
    events = tcpclient(bridgeHost, 5003, 'Timeout', 1);
    cleanup = onCleanup(@() clear('up', 'down', 'events')); %#ok<NASGU>

    frame = 320;                       % 20ms@16k
    received = int16([]);
    for i = 1:frame:numel(x)
        seg = x(i:min(i + frame - 1, numel(x)));
        write(up, seg, 'int16');
        pause(0.02);                   % 按实时速率发
        received = drain(down, received);
        drainEvents(events);
    end
    t0 = tic;                          % 收尾巴 ~3s
    while toc(t0) < 3
        received = drain(down, received);
        drainEvents(events);
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

function drainEvents(events)
    n = events.NumBytesAvailable;
    if n > 0
        raw = char(read(events, n, 'uint8')');
        fprintf('%s', raw);
    end
end
