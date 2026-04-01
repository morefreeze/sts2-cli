using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Models;

namespace Sts2HeadlessCore.Core;

/// <summary>
/// Card selector that creates a pending selection decision point.
/// When the game needs the player to choose cards (upgrade, remove, transform, bundle pick),
/// this stores the options and waits for the main loop to provide the answer.
/// </summary>
internal class HeadlessCardSelector : MegaCrit.Sts2.Core.TestSupport.ICardSelector
{
    // Pending card selection — set by game engine, read by main loop
    public List<CardModel>? PendingOptions { get; private set; }
    public int PendingMinSelect { get; private set; }
    public int PendingMaxSelect { get; private set; }
    public string PendingPrompt { get; private set; } = "";
    private TaskCompletionSource<IEnumerable<CardModel>>? _pendingTcs;

    public bool HasPending => _pendingTcs != null && !_pendingTcs.Task.IsCompleted;

    public Task<IEnumerable<CardModel>> GetSelectedCards(
        IEnumerable<CardModel> options, int minSelect, int maxSelect)
    {
        var optList = options.ToList();
        if (optList.Count == 0)
            return Task.FromResult<IEnumerable<CardModel>>(Array.Empty<CardModel>());

        // If only one option and minSelect requires it, auto-select
        if (optList.Count == 1 && minSelect >= 1)
            return Task.FromResult<IEnumerable<CardModel>>(optList);

        // Store pending selection and wait
        PendingOptions = optList;
        PendingMinSelect = minSelect;
        PendingMaxSelect = maxSelect;
        _pendingTcs = new TaskCompletionSource<IEnumerable<CardModel>>();

        Console.Error.WriteLine($"[SIM] Card selection pending: {optList.Count} options, select {minSelect}-{maxSelect}");

        // Return the task — the main loop will complete it
        return _pendingTcs.Task;
    }

    public void ResolvePending(IEnumerable<CardModel> selected)
    {
        _pendingTcs?.TrySetResult(selected);
        PendingOptions = null;
        _pendingTcs = null;
    }

    public void ResolvePendingByIndices(int[] indices)
    {
        if (PendingOptions == null) return;
        var selected = indices
            .Where(i => i >= 0 && i < PendingOptions.Count)
            .Select(i => PendingOptions[i])
            .ToList();
        ResolvePending(selected);
    }

    public void CancelPending()
    {
        _pendingTcs?.TrySetResult(Array.Empty<CardModel>());
        PendingOptions = null;
        _pendingTcs = null;
    }

    // Pending card reward from events (GetSelectedCardReward blocks until resolved)
    public List<MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult>? PendingRewardCards { get; private set; }
    private ManualResetEventSlim? _rewardWait;
    private int _rewardChoice = -1;

    public CardModel? GetSelectedCardReward(
        IReadOnlyList<MegaCrit.Sts2.Core.Entities.Cards.CardCreationResult> options,
        IReadOnlyList<CardRewardAlternative> alternatives)
    {
        if (options.Count == 0) return null;

        // Store pending and block until main loop resolves
        PendingRewardCards = options.ToList();
        _rewardChoice = -1;
        _rewardWait = new ManualResetEventSlim(false);

        Console.Error.WriteLine($"[SIM] Card reward pending: {options.Count} cards (blocking)");
        _rewardWait.Wait(TimeSpan.FromSeconds(300)); // Wait up to 5 min

        var choice = _rewardChoice;
        PendingRewardCards = null;
        _rewardWait = null;

        if (choice >= 0 && choice < options.Count)
            return options[choice].Card;
        return null;  // Skip
    }

    public bool HasPendingReward => PendingRewardCards != null && _rewardWait != null;

    public void ResolveReward(int index)
    {
        _rewardChoice = index;
        _rewardWait?.Set();
    }

    public void SkipReward()
    {
        _rewardChoice = -1;
        _rewardWait?.Set();
    }
}
