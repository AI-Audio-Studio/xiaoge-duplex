classdef Client < handle
    %xiaoge.Client  小歌全双工音频客户端(A 方案:Java-WebSocket,直连 /ws/audio)。
    %   依赖:Java-WebSocket jar + 编译好的 XiaogeWsAdapter.jar(见 java/ 与 lib/)。
    %
    %   ⚠️ 状态:**未在交付环境测试**(本机无 MATLAB/JDK)。若不想编译 Java 适配器,
    %      请改用 B 方案(XiaogeAudioBlock + xiaoge_bridge.py),那条链路已自测通过。
    %
    %   用法:
    %     c = xiaoge.Client('192.168.1.10', 8787);
    %     c.OnAudio = @(pcm) playFcn(pcm);   % pcm: int16 列向量(16k/单声道)
    %     c.OnClear = @() flushFcn();        % 打断:清空播放
    %     c.connect();
    %     c.sendPcm(int16Frame);             % 上行
    %
    %   回调(可选):OnReady(sampleRate) / OnAudio(int16) / OnClear() / OnBusy(msg)。

    properties
        OnReady = []
        OnAudio = []
        OnClear = []
        OnBusy = []
    end

    properties (Access = private)
        Ws
        Url
    end

    methods
        function obj = Client(host, port, tls)
            if nargin < 3, tls = false; end
            scheme = "ws"; if tls, scheme = "wss"; end
            obj.Url = sprintf('%s://%s:%d/ws/audio', scheme, host, port);
            obj.addJars();
        end

        function connect(obj)
            uri = java.net.URI(obj.Url);
            obj.Ws = XiaogeWsAdapter(uri);
            set(obj.Ws, 'PropertyChangeCallback', @(s, e) obj.onEvent(e));
            obj.Ws.connectBlocking();
        end

        function sendPcm(obj, pcm)
            % pcm: int16 → 转 little-endian 字节(typecast 在小端机上即小端)
            obj.Ws.sendPcm(typecast(int16(pcm(:)).', 'int8'));
        end

        function close(obj)
            if ~isempty(obj.Ws), obj.Ws.close(); end
        end
    end

    methods (Access = private)
        function addJars(~)
            here = fileparts(mfilename('fullpath'));
            libdir = fullfile(here, '..', 'lib');
            jars = dir(fullfile(libdir, '*.jar'));
            for i = 1:numel(jars)
                jp = fullfile(libdir, jars(i).name);
                if ~any(strcmp(javaclasspath, jp)), javaaddpath(jp); end
            end
        end

        function onEvent(obj, e)
            name = char(e.getPropertyName());
            val = e.getNewValue();
            switch name
                case 'Text'
                    obj.onText(char(val));
                case 'Audio'
                    if ~isempty(obj.OnAudio)
                        obj.OnAudio(typecast(int8(val).', 'int16').');
                    end
            end
        end

        function onText(obj, txt)
            if contains(txt, '"ready"')
                if ~isempty(obj.OnReady), obj.OnReady(16000); end
            elseif contains(txt, '"clear"')
                if ~isempty(obj.OnClear), obj.OnClear(); end
            elseif contains(txt, '"busy"')
                if ~isempty(obj.OnBusy), obj.OnBusy(txt); end
            end
        end
    end
end
