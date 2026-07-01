function build_xiaoge_demo(modelName)
%BUILD_XIAOGE_DEMO  程序化生成小歌 Simulink demo 模型(交付环境无法产二进制 .slx,
%   故用脚本生成)。模型:Audio Device Reader → XiaogeAudioBlock → Audio Device Writer。
%   实时麦克风/扬声器块需 Audio Toolbox;无此工具箱时仍会建块,运行时再提示。
%
%   先起桥:  python bridge/xiaoge_bridge.py 60.205.197.165 10099 --up 5001 --down 5002 --tls --insecure
%   生成:    build_xiaoge_demo            % 默认模型名 xiaoge_demo
%   然后在 Simulink 里打开运行(确保本目录在 MATLAB 路径上:addpath(pwd))。
%   状态:R2022b 适配,**未在交付环境运行**,按 README 验证。
    if nargin < 1, modelName = 'xiaoge_demo'; end
    if bdIsLoaded(modelName), close_system(modelName, 0); end

    new_system(modelName);
    load_system(modelName);
    fs = 16000; frame = 320;

    mic = [modelName '/Mic'];
    add_block('audio/Audio Device Reader', mic, ...
        'SampleRate', num2str(fs), 'SamplesPerFrame', num2str(frame), ...
        'Position', [30 100 130 150]);

    blk = [modelName '/Xiaoge'];
    add_block('simulink/User-Defined Functions/MATLAB System', blk, ...
        'System', 'XiaogeAudioBlock', 'Position', [200 95 320 155]);

    spk = [modelName '/Speaker'];
    add_block('audio/Audio Device Writer', spk, ...
        'SampleRate', num2str(fs), 'Position', [400 100 500 150]);

    add_line(modelName, 'Mic/1', 'Xiaoge/1', 'autorouting', 'on');
    add_line(modelName, 'Xiaoge/1', 'Speaker/1', 'autorouting', 'on');

    set_param(modelName, 'StopTime', 'inf', 'SolverType', 'Fixed-step', ...
        'FixedStep', num2str(frame / fs));
    save_system(modelName);
    fprintf('已生成模型 %s.slx。打开:open_system(''%s'')\n', modelName, modelName);
end
