using Sts2Advisor.Core;

public class FingerprintTests
{
    [Fact]
    public void JoinsPartsAndMarksNulls()
    {
        Assert.Equal("shop|a|-|3", Fingerprint.Of("shop", new object?[] { "a", null, 3 }));
        Assert.NotEqual(Fingerprint.Of("shop", new object?[] { 1 }), Fingerprint.Of("shop", new object?[] { 2 }));
    }
}
