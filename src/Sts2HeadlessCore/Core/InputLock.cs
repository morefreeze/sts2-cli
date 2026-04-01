using System.Threading;
using System.Threading.Tasks;

namespace Sts2HeadlessCore.Core;

/// <summary>
/// Mutual exclusion lock for input sources (Mouse vs CLI).
/// Ensures only one input source can interact with the game at a time.
/// </summary>
public class InputLock
{
    public enum InputSource { None, Mouse, CLI }

    private InputSource _owner = InputSource.None;
    private readonly SemaphoreSlim _semaphore = new(1, 1);

    /// <summary>
    /// Try to acquire the lock for the given input source.
    /// </summary>
    public bool TryAcquire(InputSource source, int timeoutMs = 5000)
    {
        if (!_semaphore.Wait(timeoutMs))
            return false;

        if (_owner != InputSource.None && _owner != source)
        {
            _semaphore.Release();
            return false;
        }

        _owner = source;
        return true;
    }

    /// <summary>
    /// Release the lock for the given input source.
    /// </summary>
    public void Release(InputSource source)
    {
        if (_owner == source)
        {
            _owner = InputSource.None;
            _semaphore.Release();
        }
    }

    /// <summary>
    /// Get the current owner of the lock.
    /// </summary>
    public InputSource CurrentOwner => _owner;

    /// <summary>
    /// Asynchronously wait for the lock to be released.
    /// </summary>
    public Task WaitUntilReleasedAsync(CancellationToken cancellationToken = default)
    {
        if (_owner == InputSource.None)
            return Task.CompletedTask;

        return Task.Run(async () =>
        {
            while (_owner != InputSource.None && !cancellationToken.IsCancellationRequested)
            {
                await Task.Delay(50, cancellationToken);
            }
        }, cancellationToken);
    }
}
