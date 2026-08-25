classdef XiaogeAudioBlock < matlab.System
    % XiaogeAudioBlock  R5.2.2 audio block for xiaoge_bridge.py.
    %   The Python bridge owns create_session, WSS Bearer auth, ctrl.hello,
    %   command ack/result, and fake executor behavior.
    %
    %   用法(先在主机起桥):
    %     python clients/matlab/bridge/xiaoge_bridge.py <create_session_url> <device_id> <credential> --up 5001 --down 5002 --events 5003
    %   再把本块拖入模型,设 BridgeHost/UpPort/DownPort/FrameSize。
    %
    %   音频格式:16000 Hz、单声道、16-bit。FrameSize 默认 320(20ms@16k)。
    %   Status:R2022b compatible; validate on your MATLAB/Simulink host.

    properties (Nontunable)
        BridgeHost = '127.0.0.1'   % 桥主机
        UpPort = 5001              % 上行 TCP 端口(麦克风→小歌)
        DownPort = 5002            % 下行 TCP 端口(小歌 TTS→此)
        FrameSize = 320            % 每步样本数
    end

    properties (Access = private)
        UpClient
        DownClient
    end

    methods (Access = protected)
        function setupImpl(obj)
            obj.UpClient = tcpclient(obj.BridgeHost, obj.UpPort, 'Timeout', 1);
            obj.DownClient = tcpclient(obj.BridgeHost, obj.DownPort, 'Timeout', 1);
        end

        function y = stepImpl(obj, u)
            write(obj.UpClient, int16(u(:)), 'int16');     % 上行
            n = obj.FrameSize;
            y = zeros(n, 1, 'int16');
            avail = floor(obj.DownClient.NumBytesAvailable / 2);
            k = min(avail, n);
            if k > 0
                y(1:k) = read(obj.DownClient, k, 'int16');  % 下行(不足补静音)
            end
        end

        function releaseImpl(obj)
            obj.UpClient = [];
            obj.DownClient = [];
        end

        function out = getOutputSizeImpl(obj)
            out = [obj.FrameSize 1];
        end

        function out = getOutputDataTypeImpl(~)
            out = 'int16';
        end

        function c = isOutputComplexImpl(~)
            c = false;
        end

        function s = isOutputFixedSizeImpl(~)
            s = true;
        end
    end
end
