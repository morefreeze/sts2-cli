// src/Sts2Advisor/AutoPilot/MapHandler.cs
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>Travels to the advised map node.</summary>
internal sealed class MapHandler(IAutoPilotHost host) : IHandler
{
    public TimeSpan Timeout => TimeSpan.FromSeconds(60);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var map = NMapScreen.Instance ?? throw new AutoPilotException("map screen gone");
        var advice = await host.FetchAdviceAsync(ct);
        if (advice.Plan.Kind != PlanKind.MapNode)
            throw new AutoPilotException($"map screen, but the advice is {advice.Plan.Kind} for {advice.Capture.Header.Decision}");
        int col = advice.Plan.Col, row = advice.Plan.Row;
        if (!advice.Capture.Choices.OfType<MapPoint>().Any(p => p.coord.col == col && p.coord.row == row))
            throw new AutoPilotException($"map: ({col},{row}) is not a reachable node");
        var point = UiHelper.FindAll<NMapPoint>(map).FirstOrDefault(p => p.Point.coord.col == col && p.Point.coord.row == row)
            ?? throw new AutoPilotException($"map: no node drawn at ({col},{row})");
        await WaitHelper.Until(() => point.IsEnabled, ct, AutoSlayConfig.mapPointEnabledTimeout, "Map point not enabled");
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnEntered() => entered.TrySetResult();
        RunManager.Instance.RoomEntered += OnEntered;
        try
        {
            host.StepTaken(advice);
            await UiHelper.Click(point);
            await WaitHelper.ForTask(entered.Task, ct, AutoSlayConfig.mapScreenTimeout, "Room not entered after map click");
        }
        finally
        {
            RunManager.Instance.RoomEntered -= OnEntered;
        }
    }
}
